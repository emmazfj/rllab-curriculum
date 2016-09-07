


import numpy as np
import tensorflow as tf
from collections import OrderedDict

from rllab.algos.base import RLAlgorithm
from rllab.misc import logger
from rllab.misc import ext
from rllab.misc.tensor_utils import flatten_tensors, unflatten_tensors
from sandbox.rocky.tf.core.parameterized import JointParameterized
from rllab.envs.grid_world_env import GridWorldEnv
from sandbox.rocky.tf.misc import tensor_utils
from sandbox.rocky.tf.regressors.categorical_mlp_regressor import CategoricalMLPRegressor
from sandbox.rocky.tf.regressors.gaussian_mlp_regressor import GaussianMLPRegressor
from sandbox.rocky.tf.optimizers.first_order_optimizer import FirstOrderOptimizer
from sandbox.rocky.hrl.envs.compound_action_sequence_env import CompoundActionSequenceEnv
from rllab.optimizers.minibatch_dataset import BatchDataset
from sandbox.rocky.tf.envs.base import TfEnv
import sandbox.rocky.tf.core.layers as L
from sandbox.rocky.tf.spaces.discrete import Discrete
from sandbox.rocky.tf.spaces.product import Product
from sandbox.rocky.tf.core.network import ConvMergeNetwork
from sandbox.rocky.tf.core.network import MLP
from sandbox.rocky.tf.core.network import GRUNetwork
from rllab.envs.base import EnvSpec
from sandbox.rocky.tf.policies.categorical_mlp_policy import CategoricalMLPPolicy
from sandbox.rocky.tf.policies.uniform_control_policy import UniformControlPolicy
from sandbox.rocky.tf.policies.base import StochasticPolicy
from sandbox.rocky.tf.core.layers_powered import LayersPowered
from sandbox.rocky.tf.distributions.diagonal_gaussian import DiagonalGaussian
from sandbox.rocky.tf.distributions.categorical import Categorical
from sandbox.rocky.tf.spaces.box import Box
from rllab.core.serializable import Serializable
from rllab.misc import special
from sandbox.rocky.hrl_imitation.envs.dummy_vec_env import DummyVecEnv
import itertools

from rllab.spaces.box import Box as TheanoBox
from rllab.envs.base import Step

AGENT = 0
GOAL = 1
N_OBJECT_TYPES = 2


class ImageGridWorld(GridWorldEnv):
    def __init__(self, desc):
        super(ImageGridWorld, self).__init__(desc)
        self._observation_space = TheanoBox(low=0., high=1., shape=(self.n_row, self.n_col, N_OBJECT_TYPES))
        self._original_obs_space = GridWorldEnv.observation_space.fget(self)

    @property
    def observation_space(self):
        return self._observation_space

    def reset(self):
        super(ImageGridWorld, self).reset()
        return self.get_current_obs()

    def step(self, action):
        _, reward, done, info = super(ImageGridWorld, self).step(action)
        agent_state = self._original_obs_space.flatten(self.state)
        return Step(self.get_current_obs(), reward, done, **dict(info, agent_state=agent_state))

    def get_current_obs(self):
        ret = np.zeros(self._observation_space.shape)
        ret[self.desc == 'G', GOAL] = 1
        cur_x = self.state / self.n_col
        cur_y = self.state % self.n_col
        ret[cur_x, cur_y, AGENT] = 1
        return ret


class SeqGridPolicyModule(object):
    """
    low-level policy receives partial observation. stochastic bottleneck
    """

    def __init__(self, low_policy_obs='full', log_prob_tensor_std=1.0):
        self.low_policy_obs = low_policy_obs
        self.log_prob_tensor_std = log_prob_tensor_std

    def new_high_policy(self, env_spec, subgoal_dim):
        subgoal_space = Discrete(subgoal_dim)
        return CategoricalMLPPolicy(
            name="high_policy",
            env_spec=EnvSpec(
                observation_space=env_spec.observation_space,
                action_space=subgoal_space,
            ),
            prob_network=ConvMergeNetwork(
                name="high_policy_network",
                input_shape=env_spec.observation_space.components[0].shape,
                extra_input_shape=(Product(env_spec.observation_space.components[1:]).flat_dim,),
                output_dim=subgoal_dim,
                hidden_sizes=(100, 100),
                conv_filters=(10, 10),
                conv_filter_sizes=(3, 3),
                conv_strides=(1, 1),
                conv_pads=('SAME', 'SAME'),
                extra_hidden_sizes=(100,),
                hidden_nonlinearity=tf.nn.tanh,
                output_nonlinearity=tf.nn.softmax,
            ),
        )

    def new_alt_high_policy(self, env_spec, subgoal_dim):
        subgoal_space = Discrete(subgoal_dim)
        return UniformControlPolicy(
            env_spec=EnvSpec(
                observation_space=env_spec.observation_space,
                action_space=subgoal_space,
            ),
        )

    def new_low_policy(self, env_spec, subgoal_dim, bottleneck_dim):
        subgoal_space = Discrete(subgoal_dim)
        if self.low_policy_obs == 'full':
            return BranchingCategoricalMLPPolicy(
                name="low_policy",
                env_spec=EnvSpec(
                    observation_space=Product(env_spec.observation_space, subgoal_space),
                    action_space=env_spec.action_space,
                ),
                shared_network=ConvMergeNetwork(
                    name="low_policy_shared_network",
                    input_shape=env_spec.observation_space.components[0].shape,
                    extra_input_shape=(Product(env_spec.observation_space.components[1:]).flat_dim,),
                    output_dim=100,
                    hidden_sizes=(100,),
                    conv_filters=(10, 10),
                    conv_filter_sizes=(3, 3),
                    conv_strides=(1, 1),
                    conv_pads=('SAME', 'SAME'),
                    extra_hidden_sizes=(100,),
                    hidden_nonlinearity=tf.nn.tanh,
                    output_nonlinearity=tf.nn.tanh,
                ),
                subgoal_dim=subgoal_dim,
                hidden_sizes=(100,),
                hidden_nonlinearity=tf.nn.tanh,
                bottleneck_dim=bottleneck_dim,
                log_prob_tensor_std=self.log_prob_tensor_std,
            )
        elif self.low_policy_obs == 'partial':
            return IgnorantBranchingCategoricalMLPPolicy(
                name="low_policy",
                env_spec=EnvSpec(
                    observation_space=Product(env_spec.observation_space, subgoal_space),
                    action_space=env_spec.action_space,
                ),
                subgoal_dim=subgoal_dim,
                hidden_sizes=(100,),
                hidden_nonlinearity=tf.nn.tanh,
                bottleneck_dim=bottleneck_dim,
                log_prob_tensor_std=self.log_prob_tensor_std,
            )
        else:
            raise NotImplementedError


class ProductMappingLayer(L.MergeLayer):
    def __init__(self, bottleneck_layer, subgoal_layer, name, num_units, nonlinearity, **kwargs):
        super(ProductMappingLayer, self).__init__([bottleneck_layer, subgoal_layer], name, **kwargs)
        self.bottleneck_dim = bottleneck_dim = bottleneck_layer.output_shape[-1]
        self.subgoal_dim = subgoal_dim = subgoal_layer.output_shape[-1]
        self.num_units = num_units
        self.nonlinearity = nonlinearity
        self.param = self.add_param(tf.random_normal_initializer(), (bottleneck_dim, subgoal_dim, num_units), "param")

    def get_output_shape_for(self, input_shapes):
        import ipdb;
        ipdb.set_trace()

    def get_output_for(self, inputs, **kwargs):
        bottleneck, subgoal = inputs
        x = tf.matmul(bottleneck, tf.reshape(self.param, (self.bottleneck_dim, -1)))
        x = tf.reshape(x, (-1, self.subgoal_dim, self.num_units))
        x = tf.reduce_sum(tf.tile(tf.expand_dims(subgoal, -1), (1, 1, self.num_units)) * x, 2)
        x = self.nonlinearity(x)
        return x


class BranchingCategoricalMLPPolicy(StochasticPolicy, LayersPowered, Serializable):
    def __init__(
            self,
            name,
            env_spec,
            shared_network,
            subgoal_dim,
            bottleneck_dim,
            bottleneck_std_threshold=1e-3,
            hidden_sizes=(32, 32),
            hidden_nonlinearity=tf.nn.tanh,
            log_prob_tensor_std=1.0,
    ):
        """
        :param env_spec: A spec for the mdp.
        :param hidden_sizes: list of sizes for the fully connected hidden layers
        :param hidden_nonlinearity: nonlinearity used for each hidden layer
        :param prob_network: manually specified network for this policy, other network params
        are ignored
        :return:
        """
        Serializable.quick_init(self, locals())

        assert isinstance(env_spec.action_space, Discrete)

        with tf.variable_scope(name):
            l_last = shared_network.output_layer

            action_dim = env_spec.action_space.flat_dim

            l_bottleneck_prob = L.DenseLayer(
                l_last,
                num_units=bottleneck_dim,
                nonlinearity=tf.nn.softmax,
                name="bottleneck"
            )

            log_prob_tensor = tf.Variable(
                initial_value=np.cast['float32'](
                    np.random.normal(
                        scale=log_prob_tensor_std, size=(bottleneck_dim, subgoal_dim, action_dim)
                    )
                ),
                trainable=True,
                name="log_prob"
            )

            prob_tensor = tf.reshape(
                tf.nn.softmax(tf.reshape(log_prob_tensor, (-1, action_dim))),
                (bottleneck_dim, subgoal_dim, action_dim)
            )

            aux_log_p_g_z_tensor = tf.Variable(
                initial_value=np.cast['float32'](
                    np.random.normal(
                        scale=log_prob_tensor_std, size=(bottleneck_dim, subgoal_dim),
                    )
                ),
                trainable=True,
                name="log_p_g_z"
            )
            aux_p_g_z_tensor = tf.reshape(
                tf.nn.softmax(tf.reshape(aux_log_p_g_z_tensor, (1, -1))),
                (bottleneck_dim, subgoal_dim)
            )

            self.l_obs = [x for x in L.get_all_layers(shared_network.input_layer) if isinstance(x, L.InputLayer)][0]
            self.l_bottleneck_prob = l_bottleneck_prob
            self.bottleneck_dim = bottleneck_dim
            self.log_prob_tensor = log_prob_tensor
            self.prob_tensor = prob_tensor
            self.aux_log_p_g_z_tensor = aux_log_p_g_z_tensor
            self.aux_p_g_z_tensor = aux_p_g_z_tensor

            self.bottleneck_dist = bottleneck_dist = Categorical(dim=bottleneck_dim)
            self.subgoal_dim = subgoal_dim
            self.dist = Categorical(env_spec.action_space.n)
            self.shared_network = shared_network

            self.bottleneck_space = bottleneck_space = Box(low=-1, high=1, shape=(bottleneck_dim,))

            super(BranchingCategoricalMLPPolicy, self).__init__(env_spec)
            LayersPowered.__init__(self, [l_bottleneck_prob])

            obs_var = self.observation_space.new_tensor_variable(
                "obs",
                extra_dims=1,
            )

            self.f_bottleneck_prob = tensor_utils.compile_function(
                [self.l_obs.input_var],
                L.get_output(l_bottleneck_prob),
            )

    def get_params_internal(self, **tags):
        return super(BranchingCategoricalMLPPolicy, self).get_params_internal(**tags) + \
               [self.log_prob_tensor, self.aux_log_p_g_z_tensor]

    def bottleneck_prob_sym(self, high_obs_var):
        return L.get_output(self.l_bottleneck_prob, {self.l_obs: high_obs_var})

    def dist_info_sym(self, obs_var, state_info_vars):
        subgoals = obs_var[:, self.observation_space.flat_dim - self.subgoal_dim:]
        bottleneck = state_info_vars["bottleneck"]
        probs = L.get_output(self.l_prob, {self.l_bottleneck_in: bottleneck, self.l_subgoal_in: subgoals})
        return dict(prob=probs)

    def dist_info(self, obs):
        high_obs = obs[:, :self.observation_space.flat_dim - self.subgoal_dim]
        subgoals = obs[:, self.observation_space.flat_dim - self.subgoal_dim:]
        bottleneck_prob = self.f_bottleneck_prob(high_obs)
        prob_tensor = self.prob_tensor.eval()
        x = bottleneck_prob.dot(prob_tensor.reshape(self.bottleneck_dim, -1))
        x = x.reshape((-1, self.subgoal_dim, self.action_space.flat_dim))
        x = np.sum(np.tile(np.expand_dims(subgoals, -1), (1, 1, self.action_space.flat_dim)) * x, 1)
        return dict(prob=x)

    # The return value is a pair. The first item is a matrix (N, A), where each
    # entry corresponds to the action value taken. The second item is a vector
    # of length N, where each entry is the density value for that action, under
    # the current policy
    def get_action(self, observation):
        bottleneck_prob = self.f_bottleneck_prob(observation)
        import ipdb;
        ipdb.set_trace()
        bottleneck_epsilon = np.random.normal(size=(self.bottleneck_dim,))
        flat_obs = self.observation_space.flatten(observation)
        dist_info = self.dist_info([flat_obs], dict(bottleneck_epsilon=[bottleneck_epsilon]))
        act = special.weighted_sample(dist_info["prob"], list(range(self.action_space.n)))
        return act, dist_info

    def get_actions(self, observations):
        N = len(observations)
        bottleneck_epsilon = np.random.normal(size=(N, self.bottleneck_dim))
        flat_obses = self.observation_space.flatten_n(observations)
        dist_info = self.dist_info(flat_obses, dict(bottleneck_epsilon=bottleneck_epsilon))
        act = [special.weighted_sample(p, list(range(self.action_space.n))) for p in dist_info["prob"]]
        return act, dist_info

    @property
    def distribution(self):
        return self.dist


class IgnorantBranchingCategoricalMLPPolicy(BranchingCategoricalMLPPolicy, Serializable):
    def __init__(
            self,
            name,
            env_spec,
            # shared_network,
            subgoal_dim,
            bottleneck_dim,
            bottleneck_std_threshold=1e-3,
            hidden_sizes=(32, 32),
            hidden_nonlinearity=tf.nn.tanh,
            **kwargs
    ):
        Serializable.quick_init(self, locals())
        l_in = L.InputLayer(shape=(None, env_spec.observation_space.components[0].flat_dim), name="input")
        slice_start = env_spec.observation_space.components[0].components[0].flat_dim
        slice_end = env_spec.observation_space.components[0].flat_dim

        l_sliced_in = L.SliceLayer(l_in, name="sliced_input", indices=slice(slice_start, slice_end), axis=-1)

        shared_network = MLP(
            input_shape=(slice_end - slice_start,),
            input_layer=l_sliced_in,
            hidden_sizes=(100, 100),
            hidden_nonlinearity=tf.nn.tanh,
            output_dim=slice_end - slice_start,
            output_nonlinearity=tf.nn.tanh,
            name="dummy_shared",
        )
        BranchingCategoricalMLPPolicy.__init__(self, name=name, env_spec=env_spec, subgoal_dim=subgoal_dim,
                                               bottleneck_dim=bottleneck_dim, shared_network=shared_network,
                                               bottleneck_std_threshold=bottleneck_std_threshold,
                                               hidden_sizes=hidden_sizes, hidden_nonlinearity=hidden_nonlinearity,
                                               **kwargs)


def weighted_sample_n(prob_matrix, items):
    s = prob_matrix.cumsum(axis=1)
    r = np.random.rand(prob_matrix.shape[0])
    k = (s < r.reshape((-1, 1))).sum(axis=1)
    n_items = len(items)
    return items[np.minimum(k, n_items - 1)]


class FixedClockPolicy(StochasticPolicy, Serializable):
    def __init__(self, env_spec, high_policy, low_policy, subgoal_interval):
        Serializable.quick_init(self, locals())
        self.high_policy = high_policy
        self.low_policy = low_policy
        self.ts = None
        self.subgoals = None
        self.subgoal_interval = subgoal_interval
        super(FixedClockPolicy, self).__init__(env_spec=env_spec)
        # obs_var =
        # self.f_action_prob = self.action_prob_sym()

    def get_params_internal(self, **tags):
        return self.high_policy.get_params(**tags) + self.low_policy.get_params(**tags)

    def reset(self, dones=None):
        self.high_policy.reset(dones)
        self.low_policy.reset(dones)
        if dones is None:
            dones = [True]
        dones = np.asarray(dones)
        if self.ts is None or len(dones) != len(self.ts):
            self.ts = np.array([-1] * len(dones))
            self.subgoals = np.zeros((len(dones),))
        self.ts[dones] = -1
        self.subgoals[dones] = np.nan

    def get_action(self, observation):
        actions, infos = self.get_actions([observation])
        return actions[0], {k: v[0] for k, v in infos.items()}

    def get_actions(self, observations):
        self.ts += 1
        subgoals, _ = self.high_policy.get_actions(observations)
        update_mask = self.ts % self.subgoal_interval == 0
        self.subgoals[update_mask] = np.asarray(subgoals)[update_mask]
        flat_obs = self.observation_space.flatten_n(observations)

        bottleneck_probs = self.low_policy.f_bottleneck_prob(flat_obs)

        # bottlenecks = [special.weighted_sample(p, range(self.low_policy.bottleneck_dim)) for p in bottleneck_probs]
        bottlenecks = weighted_sample_n(bottleneck_probs, np.arange(self.low_policy.bottleneck_dim))
        action_prob_tensor = self.low_policy.prob_tensor.eval()
        # import ipdb; ipdb.set_trace()
        action_probs = action_prob_tensor[np.cast['int'](bottlenecks), np.cast['int'](self.subgoals)]
        actions = weighted_sample_n(action_probs, np.arange(self.action_space.flat_dim))
        return actions, dict()

    def lookup_action_prob_sym(self, bottleneck_var, subgoal_var):
        x = tf.matmul(bottleneck_var, tf.reshape(self.low_policy.prob_tensor, (self.low_policy.bottleneck_dim, -1)))
        x = tf.reshape(x, (-1, self.low_policy.subgoal_dim, self.low_policy.action_space.flat_dim))
        x = tf.reduce_sum(
            tf.tile(
                tf.expand_dims(subgoal_var, -1),
                (1, 1, self.low_policy.action_space.flat_dim)
            ) * x,
            1
        )
        return x

    def all_action_probs_sym(self, obs_var):
        """
        Compute symbolic action prob for all subgoals
        """
        assert obs_var.get_shape().ndims == 3
        obs_dim = self.observation_space.flat_dim
        flat_obs_var = tf.reshape(obs_var, (-1, obs_dim))
        bottleneck_probs = self.low_policy.bottleneck_prob_sym(flat_obs_var)

        x = tf.matmul(bottleneck_probs, tf.reshape(self.low_policy.prob_tensor, (self.low_policy.bottleneck_dim, -1)))
        x = tf.reshape(x, (-1, self.low_policy.subgoal_dim, self.low_policy.action_space.flat_dim))
        return x

    def subgoal_prob_sym(self, obs_var):
        first_obs = obs_var[:, 0, :]
        return self.high_policy.dist_info_sym(first_obs)["prob"]

    def action_prob_sym(self, obs_var):
        assert obs_var.get_shape().ndims == 3

        first_obs = obs_var[:, 0, :]
        subgoal_probs = self.high_policy.dist_info_sym(first_obs)["prob"]
        # tile subgoal probabilities to correct shape
        subgoal_probs = tf.reshape(
            tf.tile(
                tf.expand_dims(subgoal_probs, 1),
                (1, self.subgoal_interval, 1)
            ),
            (-1, self.low_policy.subgoal_dim)
        )
        x = self.all_action_probs_sym(obs_var)
        x = tf.reduce_sum(
            tf.tile(
                tf.expand_dims(subgoal_probs, -1),
                (1, 1, self.low_policy.action_space.flat_dim)
            ) * x,
            1
        )
        return x


def merge_grads(grads, *extra_grads_list):
    grad_dict = OrderedDict([(y, x) for x, y in grads])
    for extra_grads in extra_grads_list:
        for grad, var in extra_grads:
            if var not in grad_dict:
                grad_dict[var] = grad
            else:
                if grad is None:
                    pass
                elif grad_dict[var] is None:
                    grad_dict[var] = grad
                else:
                    grad_dict[var] += grad
    return [(y, x) for x, y in grad_dict.items()]


UP = GridWorldEnv.action_from_direction("up")
DOWN = GridWorldEnv.action_from_direction("down")
LEFT = GridWorldEnv.action_from_direction("left")
RIGHT = GridWorldEnv.action_from_direction("right")


def generate_demonstration_trajectory(size, start_pos, end_pos, action_map):
    sx, sy = start_pos
    ex, ey = end_pos
    actions = []
    if sx < ex:
        actions += action_map[DOWN] * (ex - sx)
    elif sx > ex:
        actions += action_map[UP] * (sx - ex)
    if sy < ey:
        actions += action_map[RIGHT] * (ey - sy)
    elif sy > ey:
        actions += action_map[LEFT] * (sy - ey)
    # Now we execute the list of actions in the environment
    base_map = [["."] * size for _ in range(size)]
    base_map[sx][sy] = 'S'
    base_map[ex][ey] = 'G'
    wrapped_env = ImageGridWorld(desc=base_map)
    env = TfEnv(CompoundActionSequenceEnv(wrapped_env, action_map, obs_include_history=True))
    obs = env.reset()
    observations = []
    terminal = False
    reward = 0
    for act in actions:
        next_obs, reward, terminal, _ = env.step(act)
        observations.append(obs)
        obs = next_obs
    assert terminal
    assert reward == 1
    return env, dict(
        observations=np.asarray(env.observation_space.flatten_n(observations)),
        actions=np.asarray(env.action_space.flatten_n(actions))
    )


class SeqGridExpert(object):
    def __init__(self, grid_size=5, action_map=None):
        assert grid_size == 5
        self.grid_size = grid_size
        if action_map is None:
            action_map = [
                [0, 1, 1],  # Left
                [1, 3, 3],  # Down
                [2, 2, 0],  # Right
                [3, 0, 2],  # Up
            ]
        self.action_map = action_map
        self.paths = None
        self.path_infos = None
        self.dataset = None
        self.envs = None
        self.seg_obs = None
        self.seg_actions = None

        base_map = [
            "SWGWW",
            "...WW",
            "WWWWW",
            "WWWWW",
            "WWWWW",
        ]
        # base_map = [["."] * grid_size for _ in range(grid_size)]
        # base_map[0][0] = 'S'
        # base_map[-1][-1] = 'G'
        wrapped_env = ImageGridWorld(desc=base_map)
        env = TfEnv(CompoundActionSequenceEnv(wrapped_env, action_map, obs_include_history=True))
        self.template_env = env

        self.env_spec = env.spec

    def build_dataset(self, batch_size):
        if self.dataset is None:
            paths = self.generate_demonstrations()
            observations = tensor_utils.concat_tensor_list([p["observations"] for p in paths])
            actions = tensor_utils.concat_tensor_list([p["actions"] for p in paths])
            N = observations.shape[0]
            subgoal_interval = 3
            seg_obs = observations.reshape((N / subgoal_interval, subgoal_interval, -1))
            seg_actions = actions.reshape((N / subgoal_interval, subgoal_interval, -1))
            self.dataset = BatchDataset([seg_obs, seg_actions], batch_size=batch_size)
            self.seg_obs = seg_obs
            self.seg_actions = seg_actions
        return self.dataset

    def generate_demonstrations(self):
        if self.paths is None:
            logger.log("generating paths")
            all_pos = [(x, y) for x in range(self.grid_size) for y in range(self.grid_size)]
            paths = []
            envs = []
            for start_pos, end_pos in itertools.permutations(all_pos, 2):
                # generate a demonstration trajectory from the start to the end
                env, path = generate_demonstration_trajectory(self.grid_size, start_pos, end_pos, self.action_map)
                paths.append(path)
                envs.append(env)
            logger.log("generated")
            # import ipdb; ipdb.set_trace()
            self.paths = paths
            self.envs = envs
        return self.paths

    def log_diagnostics(self, algo):
        # pass
        # logger.log("logging MI...")
        # self.log_mis(algo)
        logger.log("logging train stats...")
        self.log_train_stats(algo)
        logger.log("logging test stats...")
        self.log_test_stats(algo)
        logger.log("logging I(g;s'|s)...")
        self.log_mi_goal_state(algo)
        # logger.log("logging exact_H(g|z)...")
        # self.log_exact_ent_g_given_z(algo)


    # def log_mis(self, algo):
    #
    #     low_policy = algo.low_policy
    #
    #     # flat_obs =
    #     obs_dim = self.env_spec.observation_space.flat_dim
    #
    #     first_obs = self.seg_obs[:, 0, :]
    #     all_obs = self.seg_obs.reshape((-1, obs_dim))
    #     # actions = self.seg_actions[:, 0, :]
    #     N = all_obs.shape[0]
    #
    #     all_low_probs = []
    #
    #     bottleneck_epsilon = np.random.normal(size=(N, algo.bottleneck_dim))
    #     # use shared random numbers to reduce variance
    #     for g in xrange(algo.subgoal_dim):
    #         subgoals = np.tile(
    #             np.asarray(algo.high_policy.action_space.flatten(g)).reshape((1, -1)),
    #             (N, 1)
    #         )
    #         low_obs = np.concatenate([all_obs, subgoals], axis=-1)
    #         low_probs = algo.low_policy.dist_info(low_obs, dict(bottleneck_epsilon=bottleneck_epsilon))["prob"]
    #         all_low_probs.append(low_probs)
    #
    #     all_low_probs = np.asarray(all_low_probs)
    #     subgoals, _ = algo.high_policy.get_actions(self.env_spec.observation_space.unflatten_n(first_obs))
    #     subgoals = np.tile(
    #         np.asarray(subgoals).reshape((-1, 1)),
    #         (1, algo.subgoal_interval)
    #     ).flatten()
    #     subgoal_low_probs = all_low_probs[subgoals, np.arange(N)]
    #
    #     # flat_low_probs = all_low_probs.reshape((-1, algo.low_policy.action_space.n))
    #
    #     bottlenecks = algo.low_policy.f_bottleneck(all_obs, bottleneck_epsilon)
    #
    #     p_g_given_z = algo.g_given_z_regressor._f_prob(bottlenecks)
    #     p_a_given_g_z = np.asarray(all_low_probs)
    #
    #     p_a_given_z = np.sum((p_g_given_z.T[:, :, np.newaxis] * p_a_given_g_z), axis=0)
    #
    #     ents_a_given_g_z = [
    #         algo.low_policy.distribution.entropy(dict(prob=cond_low_probs))
    #         for cond_low_probs in all_low_probs
    #         ]
    #     # import ipdb;
    #     # ipdb.set_trace()
    #
    #     # p_a_given_s = np.mean(all_low_probs, axis=0)
    #     ent_a_given_z = np.mean(algo.low_policy.distribution.entropy(dict(prob=p_a_given_z)))
    #     ent_a_given_g_z = np.mean(np.sum(p_g_given_z.T * np.asarray(ents_a_given_g_z), axis=0))
    #     ent_a_given_subgoal_z = np.mean(algo.low_policy.distribution.entropy(dict(prob=subgoal_low_probs)))
    #
    #     mi_a_g_given_z = ent_a_given_z - ent_a_given_g_z
    #
    #     logger.record_tabular("exact_I(a;g|z)", mi_a_g_given_z)
    #     logger.record_tabular("exact_H(a|z)", ent_a_given_z)
    #     logger.record_tabular("exact_H(a|g,z)", ent_a_given_g_z)
    #     logger.record_tabular("exact_H(a|taken_g,z)", ent_a_given_subgoal_z)

    #
    def log_train_stats(self, algo):
        env_spec = self.env_spec
        trained_policy = FixedClockPolicy(env_spec=env_spec, high_policy=algo.high_policy, low_policy=algo.low_policy,
                                          subgoal_interval=algo.subgoal_interval)

        n_envs = len(self.envs)

        train_venv = DummyVecEnv(env=self.template_env, n=n_envs, envs=self.envs,
                                 max_path_length=algo.max_path_length)

        path_rewards = [None] * n_envs
        path_discount_rewards = [None] * n_envs
        obses = train_venv.reset()
        dones = np.asarray([True] * n_envs)
        for t in range(algo.max_path_length):
            trained_policy.reset(dones)
            acts, _ = trained_policy.get_actions(obses)
            next_obses, rewards, dones, _ = train_venv.step(acts)
            obses = next_obses
            for idx, done in enumerate(dones):
                if done and path_rewards[idx] is None:
                    path_rewards[idx] = rewards[idx]
                    path_discount_rewards[idx] = rewards[idx] * (algo.discount ** t)

        logger.record_tabular("AverageTrainReturn", np.mean(path_rewards))
        logger.record_tabular("AverageTrainDiscountedReturn", np.mean(path_discount_rewards))

    def log_mi_goal_state(self, algo):
        # Essentially, we want to check how well the low-level policy learns the subgoals
        if not hasattr(self, "all_flat_obs"):
            all_obs = []
            all_desired_actions = []
            for e in self.envs:
                for nav_action, action_seq in enumerate(self.action_map):
                    obs = e.reset()
                    for raw_action in action_seq:
                        next_obs, _, _, _ = e.step(raw_action)
                        all_obs.append(obs)
                        all_desired_actions.append(raw_action)
                        obs = next_obs
            self.all_flat_obs = self.env_spec.observation_space.flatten_n(all_obs)
            self.all_desired_actions = np.asarray(all_desired_actions)

        all_flat_obs = self.all_flat_obs
        all_desired_actions = self.all_desired_actions
        N = all_flat_obs.shape[0]

        subgoal_all_nav_action_probs = []
        subgoal_ents = []

        for subgoal in range(algo.subgoal_dim):
            subgoal_onehot = np.eye(algo.subgoal_dim, dtype=np.float32)[subgoal]
            all_low_obs = np.concatenate(
                [all_flat_obs, np.tile(subgoal_onehot.reshape((1, -1)), (N, 1))],
                axis=-1
            )
            action_probs = algo.low_policy.dist_info(all_low_obs)['prob']
            nav_action_probs = np.prod(action_probs[np.arange(N), all_desired_actions].reshape((-1, 4, 3)), axis=-1)
            dummy_action_prob = np.maximum(1e-8, 1. - np.sum(nav_action_probs, axis=-1))
            all_nav_action_probs = np.concatenate([nav_action_probs, dummy_action_prob.reshape((-1, 1))], axis=-1)
            subgoal_all_nav_action_probs.append(all_nav_action_probs)

            subgoal_ents.append(np.mean(Categorical(5).entropy(dict(prob=all_nav_action_probs))))

        marginal_all_nav_action_probs = np.mean(subgoal_all_nav_action_probs, axis=0)
        marginal_ent = np.mean(Categorical(5).entropy(dict(prob=marginal_all_nav_action_probs)))
        mi = marginal_ent - np.mean(subgoal_ents)

        logger.record_tabular("exact_I(g;s'|s)", mi)

    def log_test_stats(self, algo):
        env_spec = self.env_spec
        test_policy = FixedClockPolicy(env_spec=env_spec, high_policy=algo.alt_high_policy, low_policy=algo.low_policy,
                                       subgoal_interval=algo.subgoal_interval)

        n_envs = 100

        test_venv = DummyVecEnv(env=self.template_env, n=n_envs, max_path_length=algo.max_path_length)

        path_rewards = [None] * n_envs
        path_discount_rewards = [None] * n_envs
        obses = test_venv.reset()
        dones = np.asarray([True] * n_envs)
        for t in range(algo.max_path_length):
            test_policy.reset(dones)
            acts, _ = test_policy.get_actions(obses)
            next_obses, rewards, dones, _ = test_venv.step(acts)
            obses = next_obses
            for idx, done in enumerate(dones):
                if done and path_rewards[idx] is None:
                    path_rewards[idx] = rewards[idx]
                    path_discount_rewards[idx] = rewards[idx] * (algo.discount ** t)

        logger.record_tabular("AverageTestReturn", np.mean(path_rewards))
        logger.record_tabular("AverageTestDiscountedReturn", np.mean(path_discount_rewards))

    def log_exact_ent_g_given_z(self, algo):
        ent = np.mean(algo.high_policy.distribution.entropy(dict(prob=algo.p_g_given_z)))
        logger.record_tabular("exact_H(g|z)", ent)


class FixedClockImitation(RLAlgorithm):
    def __init__(
            self,
            policy_module=None,
            policy_module_cls=None,
            subgoal_dim=4,
            subgoal_interval=3,
            bottleneck_dim=10,
            batch_size=500,
            learning_rate=1e-3,
            discount=0.99,
            max_path_length=100,
            n_epochs=100,
            mi_coeff=0.,
            kl_coeff=1.,
    ):
        self.env_expert = SeqGridExpert()
        if policy_module is None:
            if policy_module_cls is None:
                policy_module_cls = SeqGridPolicyModule
            policy_module = policy_module_cls()
        self.policy_module = policy_module

        self.subgoal_dim = subgoal_dim
        self.subgoal_interval = subgoal_interval
        self.action_dim = self.env_expert.env_spec.action_space.flat_dim
        self.bottleneck_dim = bottleneck_dim
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.discount = discount
        self.max_path_length = max_path_length
        self.n_epochs = n_epochs
        self.mi_coeff = mi_coeff
        self.kl_coeff = kl_coeff

        env_spec = self.env_expert.env_spec
        self.high_policy = self.policy_module.new_high_policy(
            env_spec=env_spec,
            subgoal_dim=self.subgoal_dim
        )
        self.alt_high_policy = self.policy_module.new_alt_high_policy(
            env_spec=env_spec,
            subgoal_dim=self.subgoal_dim
        )
        self.low_policy = self.policy_module.new_low_policy(
            env_spec=env_spec,
            subgoal_dim=self.subgoal_dim,
            bottleneck_dim=self.bottleneck_dim
        )

        self.joint_policy = FixedClockPolicy(env_spec, self.high_policy, self.low_policy, subgoal_interval)

        self.g_given_z_regressor = CategoricalMLPRegressor(
            name="g_given_z_regressor",
            input_shape=(bottleneck_dim,),
            output_dim=subgoal_dim,
            use_trust_region=False,
            hidden_sizes=(200, 200),
            optimizer=FirstOrderOptimizer(max_epochs=10, verbose=True),
        )

        self.logging_info = []

        self.f_train = None

    def final_outer(self, x, y):
        ndim = x.get_shape().ndims
        x_static_shape = x.get_shape().as_list()
        y_static_shape = y.get_shape().as_list()
        prior_shape = tf.shape(x)[:ndim - 1]
        last_shape_x = tf.shape(x)[ndim - 1]
        last_shape_y = tf.shape(y)[ndim - 1]
        flat_x = tf.reshape(x, tf.pack([-1, last_shape_x]))
        flat_y = tf.reshape(y, tf.pack([-1, last_shape_y]))
        flat_outer = tf.expand_dims(flat_x, -1) * tf.expand_dims(flat_y, 1)
        reshaped_outer = tf.reshape(flat_outer, tf.concat(0, [prior_shape, tf.shape(flat_outer)[1:]]))
        static_prior_shape = x_static_shape[:-1]
        static_last_shape_x = x_static_shape[-1]
        static_last_shape_y = y_static_shape[-1]
        reshaped_outer.set_shape(static_prior_shape + [static_last_shape_x, static_last_shape_y])
        return reshaped_outer
        # flat_x = tf.reshape(x, tf.pack([-1, last_shape])):weighted_sample_n()

    def outer_probs(self, input_probs):
        # shape: (N/K)*K*|G|*|A|
        assert input_probs.get_shape().as_list()[1] == 3
        prob_0 = input_probs[:, 0, :, :]
        prob_1 = input_probs[:, 1, :, :]
        prob_2 = input_probs[:, 2, :, :]

        x = self.final_outer(prob_0, prob_1)
        static_prior_shape = x.get_shape().as_list()[:2]
        prior_shape = tf.shape(x)[:2]
        x = tf.reshape(x, tf.concat(0, [prior_shape, [-1]]))
        x.set_shape(static_prior_shape + [self.action_dim * self.action_dim])
        x = self.final_outer(x, prob_2)
        x = tf.reshape(x, tf.concat(0, [prior_shape, [self.action_dim, self.action_dim, self.action_dim]]))
        x.set_shape(static_prior_shape + [self.action_dim, self.action_dim, self.action_dim])
        return x

    def surr_vlb_sym(self, obs_var, action_var):
        """
        Compute the variational lower bound of p(action|state)
        """
        env_spec = self.env_expert.env_spec
        obs_dim = env_spec.observation_space.flat_dim
        action_dim = env_spec.action_space.flat_dim

        # flat_obs_var = tf.reshape(obs_var, (-1, obs_dim))
        flat_action_var = tf.reshape(action_var, (-1, action_dim))

        high_obs_var = obs_var[:, 0, :]
        flat_obs_var = tf.reshape(obs_var, (-1, obs_dim))
        policy_subgoal_dist = self.high_policy.dist_info_sym(high_obs_var, dict())
        # action_prob_sym = self.joint_policy.action_prob_sym(obs_var)

        all_action_prob = self.joint_policy.all_action_probs_sym(obs_var)
        N = tf.shape(all_action_prob)[0]

        grouped_action_probs = tf.reshape(
            all_action_prob,
            (N / self.subgoal_interval, self.subgoal_interval, self.subgoal_dim, self.action_dim)
        )
        # shape: (N/K)*K*|G|*|A|
        grouped_action_probs.set_shape((None, self.subgoal_interval, self.subgoal_dim, self.action_dim))

        grouped_joint_action_probs = self.outer_probs(grouped_action_probs)

        action_var.set_shape((None, self.subgoal_interval, self.action_dim))
        # shape: (N/K)*K*1*|A|
        sup_grouped_probs = tf.cast(tf.expand_dims(action_var, 2), tf.float32)

        sup_grouped_joint_probs = self.outer_probs(sup_grouped_probs)

        subgoal_prob = policy_subgoal_dist["prob"]

        grouped_joint_subgoal_prob = tf.reshape(subgoal_prob, (-1, self.subgoal_dim, 1, 1, 1))

        marginal_joint_action_probs = tf.reduce_sum(grouped_joint_subgoal_prob * grouped_joint_action_probs, 1,
                                                    keep_dims=True)

        vlb = sup_grouped_joint_probs * tf.log(marginal_joint_action_probs + 1e-8)
        vlb = tf.reduce_mean(
            tf.reduce_sum(tf.reshape(vlb, [-1, self.action_dim * self.action_dim * self.action_dim]), -1)
        ) / self.subgoal_interval

        surr_vlb = vlb

        marginal_p_g = tf.reduce_mean(subgoal_prob, reduction_indices=0, keep_dims=True)
        p_z_given_s = self.low_policy.bottleneck_prob_sym(flat_obs_var)
        marginal_p_z = tf.reduce_mean(p_z_given_s, reduction_indices=0, keep_dims=True)

        self.logging_info.extend([
            ("average_H(p(g|s))", tf.reduce_mean(self.high_policy.distribution.entropy_sym(policy_subgoal_dist))),
            ("average_H(p(g))", tf.reduce_mean(self.high_policy.distribution.entropy_sym(dict(prob=marginal_p_g)))),
            ("average_H(p(z|s))", tf.reduce_mean(self.low_policy.bottleneck_dist.entropy_sym(dict(prob=p_z_given_s)))),
            ("average_H(p(z))", tf.reduce_mean(self.low_policy.bottleneck_dist.entropy_sym(dict(prob=marginal_p_z)))),
        ])

        return vlb, surr_vlb

    def p_g_given_z_sym(self, obs_var):
        # we now need to compute p(g|z) = p(g,z)/p(z)
        # where p(g,z) = sum_s p(g|s)p(z|s)p(s)
        # p(g|s)
        env_spec = self.env_expert.env_spec
        obs_dim = env_spec.observation_space.flat_dim
        high_obs = obs_var[:, 0, :]
        flat_obs_var = tf.reshape(obs_var, (-1, obs_dim))
        subgoal_probs = self.high_policy.dist_info_sym(high_obs)["prob"]

        # p(z|s)
        bottleneck_probs = self.low_policy.bottleneck_prob_sym(flat_obs_var)
        N = tf.shape(bottleneck_probs)[0]
        flat_subgoal_probs = tf.reshape(
            tf.tile(tf.expand_dims(subgoal_probs, 1), (1, self.subgoal_interval, 1)),
            (-1, self.subgoal_dim)
        )
        p_z = tf.reduce_mean(bottleneck_probs, reduction_indices=0)
        p_g_z = tf.matmul(tf.transpose(flat_subgoal_probs), bottleneck_probs) / tf.cast(N, tf.float32)
        p_g_given_z = p_g_z / tf.expand_dims(p_z, 0)  # tf.reduce_sum(p_g_z, reduction_indices=0, keep_dims=True)
        return p_g_given_z, p_g_z, p_z

    def new_mi_a_g_given_z_sym(self, obs_var, action_var):
        env_spec = self.env_expert.env_spec
        obs_dim = env_spec.observation_space.flat_dim
        action_dim = env_spec.action_space.flat_dim

        flat_obs_var = tf.reshape(obs_var, (-1, obs_dim))

        # compute p(z), a vector of dimension |Z|
        # p_z = tf.reduce_mean(self.low_policy.bottleneck_prob_sym(flat_obs_var))

        # compute p(g|z), p(g,z), and p(z), which are, respectively:
        # p(g|z): a matrix of dimension |G|*|Z|
        # p(g,z): a matrix of dimension |G|*|Z|
        # p(z): a vector of dimension |Z|
        p_g_given_z, p_g_z, p_z = self.p_g_given_z_sym(obs_var)

        # this is of dimension |G|*|Z|
        aux_p_g_z = tf.transpose(self.low_policy.aux_p_g_z_tensor)
        aux_p_z = tf.reduce_sum(aux_p_g_z, reduction_indices=0)
        aux_p_g_given_z = aux_p_g_z / tf.expand_dims(aux_p_z, 0)

        # retrieve p(a|g,z), which is a tensor of dimension |Z|*|G|*|A|
        # we first transpose it so it's |G|*|Z|*|A|
        p_a_given_g_z = tf.transpose(self.low_policy.prob_tensor, (1, 0, 2))

        # Now, compute the entropy so we get H(A|g,z). This should be a matrix of size |G|*|Z|
        ent_A_given_g_z = tf.reshape(
            self.low_policy.distribution.entropy_sym(dict(prob=tf.reshape(p_a_given_g_z, (-1, action_dim)))),
            (self.subgoal_dim, self.bottleneck_dim)
        )

        ##########
        ### The following quantities are all based on aux_p(g,z)
        ##########

        # Now, we take expectation to obtain H(A|G,Z). This should be a single scalar
        aux_ent_A_given_G_Z = tf.reduce_sum(aux_p_g_z * ent_A_given_g_z)

        # compute p(a|z). we just need to marginalize p(a|g,z) over p(g|z). should be a matrix of size |Z|*|A|
        aux_p_a_given_z = tf.reduce_sum(p_a_given_g_z * tf.expand_dims(aux_p_g_given_z, 2), 0)
        # compute H(A|z)
        aux_ent_A_given_z = self.low_policy.distribution.entropy_sym(dict(prob=aux_p_a_given_z))
        # compute H(A|Z)
        aux_ent_A_given_Z = tf.reduce_sum(aux_ent_A_given_z * aux_p_z)

        aux_mi = aux_ent_A_given_Z - aux_ent_A_given_G_Z

        aux_ent_h_given_z = self.high_policy.distribution.entropy_sym(dict(prob=tf.transpose(aux_p_g_given_z)))
        aux_ent_h_given_z = tf.reduce_sum(aux_ent_h_given_z * aux_p_z)

        ##########
        ### Compute actual quantities
        ##########

        # Now, we take expectation to obtain H(A|G,Z). This should be a single scalar
        ent_A_given_G_Z = tf.reduce_sum(p_g_z * ent_A_given_g_z)

        # compute p(a|z). we just need to marginalize p(a|g,z) over p(g|z). should be a matrix of size |Z|*|A|
        p_a_given_z = tf.reduce_sum(p_a_given_g_z * tf.expand_dims(p_g_given_z, 2), 0)
        # compute H(A|z)
        ent_A_given_z = self.low_policy.distribution.entropy_sym(dict(prob=p_a_given_z))
        # compute H(A|Z)
        ent_A_given_Z = tf.reduce_sum(ent_A_given_z * p_z)

        mi = ent_A_given_Z - ent_A_given_G_Z

        ent_h_given_z = self.high_policy.distribution.entropy_sym(dict(prob=tf.transpose(p_g_given_z)))
        ent_h_given_z = tf.reduce_sum(ent_h_given_z * p_z)

        self.logging_info.append(("aux_H(a|z)", aux_ent_A_given_Z))
        self.logging_info.append(("aux_H(a|g,z)", aux_ent_A_given_G_Z))
        self.logging_info.append(("aux_H(g|z)", aux_ent_h_given_z))
        self.logging_info.append(("real_H(a|z)", ent_A_given_Z))
        self.logging_info.append(("real_H(a|g,z)", ent_A_given_G_Z))
        self.logging_info.append(("real_H(g|z)", ent_h_given_z))
        # self.logging_info.append(("real_I(a;g|z)", mi))

        flat_p_g_z = tf.reshape(p_g_z, (1, -1))
        aux_flat_p_g_z = tf.reshape(aux_p_g_z, (1, -1))

        kl_g_given_z = self.high_policy.distribution.kl_sym(dict(prob=tf.transpose(p_g_given_z)),
                                                            dict(prob=tf.transpose(aux_p_g_given_z)))
        kl_z = self.low_policy.bottleneck_dist.kl_sym(dict(prob=tf.reshape(p_z, (1, -1))),
                                                      dict(prob=tf.reshape(aux_p_z, (1, -1))))

        joint_kl = tf.reduce_sum(kl_g_given_z * p_z) + tf.reduce_sum(kl_z)

        return aux_mi, mi, joint_kl

    def init_opt(self):
        logger.log("setting up training")

        env_spec = self.env_expert.env_spec

        obs_var = env_spec.observation_space.new_tensor_variable(
            name="obs",
            extra_dims=2,
        )
        action_var = env_spec.action_space.new_tensor_variable(
            name="action",
            extra_dims=2,
        )

        vlb, surr_vlb = self.surr_vlb_sym(obs_var, action_var)
        # self.mi_a_g_given_z_sym(obs_var, action_var)
        aux_mi_a_g_given_z, true_mi_a_g_given_z, joint_kl = self.new_mi_a_g_given_z_sym(obs_var, action_var)

        all_params = self.joint_policy.get_params(trainable=True)
        # bottleneck_params = L.get_all_params(self.low_policy.l_bottleneck, trainable=True)

        optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)

        loss = -surr_vlb - self.mi_coeff * aux_mi_a_g_given_z + self.kl_coeff * joint_kl

        all_grads = optimizer.compute_gradients(loss, var_list=all_params)
        # all_grads = _add_scaled_noise_to_gradients(all_grads, 0.01)
        # bottleneck_grads = optimizer.compute_gradients(
        #     - self.mi_coeff * surr_mi_a_g_given_z, var_list=all_params  # bottleneck_params
        # )

        # all_grads = merge_grads(vlb_grads, bottleneck_grads)

        train_op = optimizer.apply_gradients(all_grads)

        self.logging_info.extend([
            ("average_NegVlb", -vlb),
            ("average_Vlb", vlb),
            ("average_aux_I(a;g|z)", aux_mi_a_g_given_z),
            ("average_true_I(a;g|z)", true_mi_a_g_given_z),
            ("average_KL_true_aux", joint_kl),
            ("average_loss", loss),
        ])

        self.f_train = tensor_utils.compile_function(
            inputs=[obs_var, action_var],
            outputs=[train_op] + [x[1] for x in self.logging_info],
        )
        self.f_log_only = tensor_utils.compile_function(
            inputs=[obs_var, action_var],
            outputs=[x[1] for x in self.logging_info],
        )

    def get_snapshot(self):
        return dict(
            env=self.env_expert.template_env,
            policy=self.joint_policy,
            high_policy=self.high_policy,
            low_policy=self.low_policy,
        )

    def update_g_given_z_estimator(self, batch_obs, batch_actions):
        high_obs = batch_obs[:, 0, :]
        env_spec = self.env_expert.env_spec

        flat_obs = batch_obs.reshape((-1, env_spec.observation_space.flat_dim))

        # p(g|s)
        subgoal_probs = self.high_policy.dist_info(high_obs)["prob"]
        subgoal_probs = np.reshape(
            np.tile(np.expand_dims(subgoal_probs, 1), (1, self.subgoal_interval, 1)),
            (-1, self.subgoal_dim)
        )
        # p(z|s)
        bottleneck_probs = self.low_policy.f_bottleneck_prob(flat_obs)
        N = bottleneck_probs.shape[0]

        # we now need to compute p(g|z) = p(g,z)/p(z)
        # where p(g,z) = sum_s p(g|s)p(z|s)p(s)
        p_g_z = subgoal_probs.T.dot(bottleneck_probs) / N
        p_g_given_z = p_g_z / np.sum(p_g_z, axis=0, keepdims=True)

        self.p_g_given_z = p_g_given_z
        self.p_g_z = p_g_z

    def train(self):
        dataset = self.env_expert.build_dataset(self.batch_size)
        self.init_opt()
        with tf.Session() as sess:
            logger.log("initializing variables")
            sess.run(tf.initialize_all_variables())
            logger.log("initialized")

            self.update_g_given_z_estimator(self.env_expert.seg_obs, self.env_expert.seg_actions)
            sess.run(tf.assign(self.low_policy.aux_log_p_g_z_tensor, np.log(self.p_g_z).T))

            bottleneck_params = L.get_all_params(self.low_policy.l_bottleneck_prob, trainable=True)
            non_bottleneck_params = list(set(self.low_policy.get_params()) - set(L.get_all_params(
                self.low_policy.l_bottleneck_prob, trainable=True)))

            for epoch_id in range(self.n_epochs):

                logger.log("Start epoch %d..." % epoch_id)

                all_vals = []

                prev_high_params = self.high_policy.get_param_values()
                prev_low_params = self.low_policy.get_param_values()
                prev_bottleneck_params = flatten_tensors(sess.run(bottleneck_params))
                prev_non_bottleneck_params = flatten_tensors(sess.run(non_bottleneck_params))

                # for _ in xrange(10):
                for batch_obs, batch_actions in dataset.iterate():
                    N = batch_obs.shape[0]
                    # Sample minibatch and train
                    # import ipdb; ipdb.set_trace()
                    vals = self.f_train(batch_obs, batch_actions)[1:]
                    all_vals.append(vals)

                self.update_g_given_z_estimator(self.env_expert.seg_obs, self.env_expert.seg_actions)

                after_high_params = self.high_policy.get_param_values()
                after_low_params = self.low_policy.get_param_values()
                after_bottleneck_params = flatten_tensors(sess.run(bottleneck_params))
                after_non_bottleneck_params = flatten_tensors(sess.run(non_bottleneck_params))

                logger.log("Evaluating...")

                logger.record_tabular("Epoch", epoch_id)
                mean_all_vals = np.mean(np.asarray(all_vals), axis=0)
                for (k, _), v in zip(self.logging_info, mean_all_vals):
                    logger.record_tabular(k, v)

                # also log a batch version of the stats
                batch_logs = self.f_log_only(*dataset._inputs)
                for (k, _), v in zip(self.logging_info, batch_logs):
                    logger.record_tabular("batch_" + k, v)

                logger.record_tabular("dHighParamNorm", np.linalg.norm(after_high_params - prev_high_params))
                logger.record_tabular("dLowParamNorm", np.linalg.norm(after_low_params - prev_low_params))
                logger.record_tabular("dBottleneckParamNorm",
                                      np.linalg.norm(after_bottleneck_params - prev_bottleneck_params))
                logger.record_tabular("dNonBottleneckParamNorm", np.linalg.norm(after_non_bottleneck_params -
                                                                                prev_non_bottleneck_params))

                self.env_expert.log_diagnostics(self)
                logger.dump_tabular()
                logger.save_itr_params(epoch_id, self.get_snapshot())
