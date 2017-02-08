import numpy as np
import sandbox.rocky.tf.core.layers as L
import tensorflow as tf
from sandbox.rocky.tf.core.layers_powered import LayersPowered
from sandbox.rocky.tf.distributions.recurrent_categorical import RecurrentCategorical
from sandbox.rocky.tf.misc import tensor_utils
from sandbox.rocky.tf.policies.rnn_utils import create_recurrent_network
from sandbox.rocky.tf.spaces.box import Box
from sandbox.rocky.tf.spaces.discrete import Discrete
from sandbox.rocky.tf.policies.base import StochasticPolicy

from rllab.core.serializable import Serializable
from rllab.misc import special
from rllab.misc.overrides import overrides
from sandbox.rocky.tf.spaces.product import Product


def filter_summary(summary_var, terminal_var, summary_dim):
    """
    summary_var should be of shape batch_size x n_steps x summary_dim
    terminal_var should be of shape batch_size x n_steps x 1

    Here, each trajectory actually spans over multiple episodes, and terminal_var indicates whether the current
    episode has ended
    """

    # Bring the time dimension to the front
    shuffled_summary_var = tf.transpose(summary_var, (1, 0, 2))
    shuffled_terminal_var = tf.transpose(terminal_var, (1, 0, 2))

    def step(prev, cur):
        cur_summary = cur[:, :summary_dim]
        cur_terminal = cur[:, summary_dim:]
        return prev * (1 - cur_terminal) + cur_summary * cur_terminal

    filtered = tf.scan(
        step,
        elems=tf.concat(2, [shuffled_summary_var, shuffled_terminal_var]),
        initializer=shuffled_summary_var[0, :, :]
    )
    return tf.transpose(filtered, (1, 0, 2))


class CategoricalDualRNNPolicy(StochasticPolicy, LayersPowered, Serializable):
    def __init__(
            self,
            name,
            env_spec,
            hidden_dim=32,
            feature_network=None,
            state_include_action=True,
            hidden_nonlinearity=tf.tanh,
            network_type="gru",
    ):
        """
        Maintain two RNNs, one for maintaining within-episode state, and one for summarizing past episodes
        """

        with tf.variable_scope(name):
            assert isinstance(env_spec.action_space, Discrete)
            Serializable.quick_init(self, locals())
            super(CategoricalDualRNNPolicy, self).__init__(env_spec)

            obs_dim = env_spec.observation_space.flat_dim
            action_dim = env_spec.action_space.flat_dim

            if state_include_action:
                input_dim = obs_dim + action_dim
            else:
                input_dim = obs_dim

            l_input = L.InputLayer(
                shape=(None, None, input_dim),
                name="input"
            )

            if feature_network is None:
                feature_dim = input_dim
                l_flat_feature = None
                l_feature = l_input
            else:
                feature_dim = feature_network.output_layer.output_shape[-1]
                l_flat_feature = feature_network.output_layer
                l_feature = L.OpLayer(
                    l_flat_feature,
                    extras=[l_input],
                    name="reshape_feature",
                    op=lambda flat_feature, input: tf.reshape(
                        flat_feature,
                        tf.pack([tf.shape(input)[0], tf.shape(input)[1], feature_dim])
                    ),
                    shape_op=lambda _, input_shape: (input_shape[0], input_shape[1], feature_dim)
                )

            summary_network = create_recurrent_network(
                network_type,
                input_shape=(feature_dim,),
                input_layer=l_feature,
                output_dim=hidden_dim,
                hidden_dim=hidden_dim,
                hidden_nonlinearity=hidden_nonlinearity,
                output_nonlinearity=hidden_nonlinearity,
                name="summary_network"
            )

            l_summary_in = L.InputLayer(
                shape=(None, None, hidden_dim),
                name="summary_in"
            )

            # No fancy integration technique for now

            prob_network = create_recurrent_network(
                network_type,
                input_shape=(feature_dim,),
                input_layer=L.concat(2, [l_feature, l_summary_in]),
                output_dim=env_spec.action_space.n,
                hidden_dim=hidden_dim,
                hidden_nonlinearity=hidden_nonlinearity,
                output_nonlinearity=tf.nn.softmax,
                name="prob_network"
            )

            self.summary_network = summary_network
            self.prob_network = prob_network
            self.feature_network = feature_network
            self.l_input = l_input
            self.l_summary_in = l_summary_in
            self.l_feature = l_feature
            self.state_include_action = state_include_action

            flat_input_var = tf.placeholder(dtype=tf.float32, shape=(None, input_dim), name="flat_input")
            if feature_network is None:
                feature_var = flat_input_var
            else:
                feature_var = L.get_output(l_flat_feature, {feature_network.input_layer: flat_input_var})

            # self.f_step_prob = tensor_utils.compile_function(
            #     [
            #         flat_input_var,
            #         prob_network.step_prev_state_layer.input_var
            #     ],
            #     L.get_output([
            #         prob_network.step_output_layer,
            #         prob_network.step_state_layer
            #     ], {prob_network.step_input_layer: feature_var})
            # )
            #
            self.input_dim = input_dim
            self.action_dim = action_dim
            self.hidden_dim = hidden_dim
            self.state_dim = summary_network.state_dim
            #
            # self.prev_actions = None
            # self.prev_hiddens = None
            self.dist = RecurrentCategorical(env_spec.action_space.n)

            out_layers = [prob_network.output_layer, summary_network.output_layer]
            if feature_network is not None:
                out_layers.append(feature_network.output_layer)

            LayersPowered.__init__(self, out_layers)

    @overrides
    def dist_info_sym(self, obs_var, state_info_vars):
        n_batches = tf.shape(obs_var)[0]
        n_steps = tf.shape(obs_var)[1]
        obs_var = tf.reshape(obs_var, tf.pack([n_batches, n_steps, -1]))
        obs_var = tf.cast(obs_var, tf.float32)
        if self.state_include_action:
            prev_action_var = tf.cast(state_info_vars["prev_action"], tf.float32)
            all_input_var = tf.concat(2, [obs_var, prev_action_var])
        else:
            all_input_var = obs_var
        if self.feature_network is None:
            summary_var = L.get_output(
                self.summary_network.output_layer,
                {self.l_input: all_input_var}
            )
        else:
            flat_input_var = tf.reshape(all_input_var, (-1, self.input_dim))
            summary_var = L.get_output(
                self.summary_network.output_layer,
                {self.l_input: all_input_var, self.feature_network.input_layer: flat_input_var}
            )

        summary_var.set_shape((None, None, self.hidden_dim))
        # Only take the first vector for each episode, and repeat it for the rest
        # How to do this in tf hmm...
        assert isinstance(self.observation_space, Product)
        assert isinstance(self.observation_space.components[-1], Box)


        # Slice out the last component
        start_idx = self.observation_space.flat_dim - self.observation_space.components[-1].flat_dim
        end_idx = self.observation_space.flat_dim

        episode_terminal_var = obs_var[:, :, start_idx:end_idx]

        filtered_summary_var = filter_summary(summary_var, episode_terminal_var, self.hidden_dim)

        import ipdb;
        ipdb.set_trace()
        # if self.feature_network is None:
        #     return dict(
        #         prob=L.get_output(
        #             self.prob_network.output_layer,
        #             {self.l_input: all_input_var}
        #         )
        #     )
        # else:
        #     return

    @property
    def vectorized(self):
        return True

    def reset(self, dones=None):
        if dones is None:
            dones = [True]
        dones = np.asarray(dones)
        if self.prev_actions is None or len(dones) != len(self.prev_actions):
            self.prev_actions = np.zeros((len(dones), self.action_space.flat_dim))
            self.prev_hiddens = np.zeros((len(dones), self.hidden_dim))

        self.prev_actions[dones] = 0.
        self.prev_hiddens[dones] = self.prob_network.hid_init_param.eval()  # get_value()

    # The return value is a pair. The first item is a matrix (N, A), where each
    # entry corresponds to the action value taken. The second item is a vector
    # of length N, where each entry is the density value for that action, under
    # the current policy
    @overrides
    def get_action(self, observation):
        actions, agent_infos = self.get_actions([observation])
        return actions[0], {k: v[0] for k, v in agent_infos.items()}

    @overrides
    def get_actions(self, observations):
        flat_obs = self.observation_space.flatten_n(observations)
        if self.state_include_action:
            assert self.prev_actions is not None
            all_input = np.concatenate([
                flat_obs,
                self.prev_actions
            ], axis=-1)
        else:
            all_input = flat_obs
        probs, hidden_vec = self.f_step_prob(all_input, self.prev_hiddens)
        actions = special.weighted_sample_n(probs, np.arange(self.action_space.n))
        prev_actions = self.prev_actions
        self.prev_actions = self.action_space.flatten_n(actions)
        self.prev_hiddens = hidden_vec
        agent_info = dict(prob=probs)
        if self.state_include_action:
            agent_info["prev_action"] = np.copy(prev_actions)
        return actions, agent_info

    @property
    @overrides
    def recurrent(self):
        return True

    @property
    def distribution(self):
        return self.dist

    @property
    def state_info_specs(self):
        if self.state_include_action:
            return [
                ("prev_action", (self.action_dim,)),
            ]
        else:
            return []