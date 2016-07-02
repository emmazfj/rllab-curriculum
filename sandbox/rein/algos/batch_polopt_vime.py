import numpy as np

from rllab.algos.base import RLAlgorithm
from sandbox.rein.sampler import parallel_sampler_vime as parallel_sampler
from rllab.misc import special
from rllab.misc import tensor_utils
from rllab.algos import util
import rllab.misc.logger as logger
import rllab.plotter as plotter
from sandbox.rein.dynamics_models.utils import iterate_minibatches


# exploration imports
# -------------------
import theano
import lasagne
from collections import deque
import time
from sandbox.rein.dynamics_models.bnn import bnn
# -------------------


class SimpleReplayPool(object):
    """Replay pool"""

    def __init__(
            self, max_pool_size, observation_shape, action_dim,
            observation_dtype=theano.config.floatX,  # @UndefinedVariable
            action_dtype=theano.config.floatX):  # @UndefinedVariable
        self._observation_shape = observation_shape
        self._action_dim = action_dim
        self._observation_dtype = observation_dtype
        self._action_dtype = action_dtype
        self._max_pool_size = max_pool_size

        self._observations = np.zeros(
            (max_pool_size,) + observation_shape,
            dtype=observation_dtype
        )
        self._actions = np.zeros(
            (max_pool_size, action_dim),
            dtype=action_dtype
        )
        self._rewards = np.zeros(max_pool_size, dtype='float32')
        self._terminals = np.zeros(max_pool_size, dtype='uint8')
        self._bottom = 0
        self._top = 0
        self._size = 0

    def add_sample(self, observation, action, reward, terminal):
        self._observations[self._top] = observation
        self._actions[self._top] = action
        self._rewards[self._top] = reward
        self._terminals[self._top] = terminal
        self._top = (self._top + 1) % self._max_pool_size
        if self._size >= self._max_pool_size:
            self._bottom = (self._bottom + 1) % self._max_pool_size
        else:
            self._size = self._size + 1

    def random_batch(self, batch_size):
        assert self._size > batch_size
        indices = np.zeros(batch_size, dtype='uint64')
        transition_indices = np.zeros(batch_size, dtype='uint64')
        count = 0
        while count < batch_size:
            index = np.random.randint(
                self._bottom, self._bottom + self._size) % self._max_pool_size
            # make sure that the transition is valid: if we are at the end of the pool, we need to discard
            # this sample
            if index == self._size - 1 and self._size <= self._max_pool_size:
                continue
            transition_index = (index + 1) % self._max_pool_size
            indices[count] = index
            transition_indices[count] = transition_index
            count += 1
        return dict(
            observations=self._observations[indices],
            actions=self._actions[indices],
            rewards=self._rewards[indices],
            terminals=self._terminals[indices],
            next_observations=self._observations[transition_indices]
        )

    def mean_obs_act(self):
        if self._size >= self._max_pool_size:
            obs = self._observations
            act = self._actions
        else:
            obs = self._observations[:self._top + 1]
            act = self._actions[:self._top + 1]
        obs_mean = np.mean(obs, axis=0)
        obs_std = np.std(obs, axis=0)
        act_mean = np.mean(act, axis=0)
        act_std = np.std(act, axis=0)
        return obs_mean, obs_std, act_mean, act_std

    @property
    def size(self):
        return self._size


class BatchPolopt(RLAlgorithm):
    """
    Base class for batch sampling-based policy optimization methods.
    This includes various policy gradient methods like vpg, npg, ppo, trpo, etc.
    """

    def __init__(
            self,
            env,
            policy,
            baseline,
            n_itr=500,
            start_itr=0,
            batch_size=5000,
            max_path_length=500,
            discount=0.99,
            gae_lambda=1,
            plot=False,
            pause_for_plot=False,
            whole_paths=True,
            center_adv=True,
            positive_adv=False,
            record_states=False,
            store_paths=False,
            algorithm_parallelized=False,
            # exploration params
            eta=1.,
            snn_n_samples=10,
            prior_sd=0.5,
            use_kl_ratio=False,
            kl_q_len=10,
            use_reverse_kl_reg=False,
            reverse_kl_reg_factor=1e-3,
            use_replay_pool=True,
            replay_pool_size=100000,
            min_pool_size=500,
            n_updates_per_sample=500,
            pool_batch_size=10,
            n_itr_update=5,
            reward_alpha=0.001,
            kl_alpha=0.001,
            normalize_reward=False,
            kl_batch_size=1,
            use_kl_ratio_q=False,
            unn_n_hidden=[32],
            unn_layers_type=[1, 1],
            unn_learning_rate=0.001,
            second_order_update=False,
            compression=False,
            information_gain=True,
            surprise_transform=None,
            update_likelihood_sd=False,
            replay_kl_schedule=1.0,
            **kwargs
    ):
        """
        :param env: Environment
        :param policy: Policy
        :param baseline: Baseline
        :param n_itr: Number of iterations.
        :param start_itr: Starting iteration.
        :param batch_size: Number of samples per iteration.
        :param max_path_length: Maximum length of a single rollout.
        :param discount: Discount.
        :param gae_lambda: Lambda used for generalized advantage estimation.
        :param plot: Plot evaluation run after each iteration.
        :param pause_for_plot: Whether to pause before contiuing when plotting.
        :param whole_paths: Make sure that the samples contain whole trajectories, even if the actual batch size is
        slightly larger than the specified batch_size.
        :param center_adv: Whether to rescale the advantages so that they have mean 0 and standard deviation 1.
        :param positive_adv: Whether to shift the advantages so that they are always positive. When used in
        conjunction with center_adv the advantages will be standardized before shifting.
        :param store_paths: Whether to save all paths data to the snapshot.
        :return:
        """
        self.env = env
        self.policy = policy
        self.baseline = baseline
        self.n_itr = n_itr
        self.start_itr = start_itr
        self.batch_size = batch_size
        self.max_path_length = max_path_length
        self.discount = discount
        self.gae_lambda = gae_lambda
        self.plot = plot
        self.pause_for_plot = pause_for_plot
        self.whole_paths = whole_paths
        self.center_adv = center_adv
        self.positive_adv = positive_adv
        self.store_paths = store_paths

        # Set exploration params
        # ----------------------
        self.eta = eta
        self.snn_n_samples = snn_n_samples
        self.prior_sd = prior_sd
        self.use_kl_ratio = use_kl_ratio
        self.kl_q_len = kl_q_len
        self.use_reverse_kl_reg = use_reverse_kl_reg
        self.reverse_kl_reg_factor = reverse_kl_reg_factor
        self.use_replay_pool = use_replay_pool
        self.replay_pool_size = replay_pool_size
        self.min_pool_size = min_pool_size
        self.n_updates_per_sample = n_updates_per_sample
        self.pool_batch_size = pool_batch_size
        self.n_itr_update = n_itr_update
        self.reward_alpha = reward_alpha
        self.kl_alpha = kl_alpha
        self.normalize_reward = normalize_reward
        self.kl_batch_size = kl_batch_size
        self.use_kl_ratio_q = use_kl_ratio_q
        self.unn_n_hidden = unn_n_hidden
        self.unn_layers_type = unn_layers_type
        self.unn_learning_rate = unn_learning_rate
        self.second_order_update = second_order_update
        self.compression = compression
        self.information_gain = information_gain
        self.surprise_transform = surprise_transform
        self.update_likelihood_sd = update_likelihood_sd
        self.replay_kl_schedule = replay_kl_schedule
        # ----------------------

        if self.second_order_update:
            assert self.n_itr_update == 1

        # Params to keep track of moving average (both intrinsic and external
        # reward) mean/var.
        if self.normalize_reward:
            self._reward_mean = deque(maxlen=self.kl_q_len)
            self._reward_std = deque(maxlen=self.kl_q_len)
        if self.use_kl_ratio:
            self._kl_mean = deque(maxlen=self.kl_q_len)
            self._kl_std = deque(maxlen=self.kl_q_len)

        # If not Q, we use median of each batch, perhaps more stable? Because
        # network is only updated between batches, might work out well.
        if self.use_kl_ratio_q:
            # Add Queue here to keep track of N last kl values, compute average
            # over them and divide current kl values by it. This counters the
            # exploding kl value problem.
            self.kl_previous = deque(maxlen=self.kl_q_len)

    def start_worker(self):
        parallel_sampler.populate_task(self.env, self.policy, self.bnn)
        if self.plot:
            plotter.init_plot(self.env, self.policy)

    def shutdown_worker(self):
        pass

    def train(self):

        # Bayesian neural network (BNN) initialization.
        # ------------------------------------------------

        # If we don't use a replay pool, we could have correct values here, as
        # it is purely Bayesian. We then divide the KL divergence term by the
        # number of batches in each iteration `batch'. Also the batch size
        # would be given correctly.
        if self.use_replay_pool:
            batch_size = 1
            n_batches = 5  # FIXME, there is no correct value!
        else:
            batch_size = int(self.pool_batch_size)
            n_batches = int(
                np.ceil(float(self.batch_size) / self.pool_batch_size))
            n_iterations = int(
                np.ceil(float(self.n_updates_per_sample) / self.batch_size))
            logger.log(
                'Using {} BNN minibatches of size {} to train, each epoch has {} iterations.'.format(n_batches, batch_size, n_iterations))

        # MDP observation and action dimensions.
        obs_dim = np.sum(self.env.observation_space.shape)
        act_dim = self.env.action_dim

        logger.log("Building BNN model (eta={}) ...".format(self.eta))
        start_time = time.time()

        self.bnn = bnn.BNN(
            n_in=(obs_dim + act_dim),
            n_hidden=self.unn_n_hidden,
            n_out=obs_dim,
            n_batches=n_batches,
            layers_type=self.unn_layers_type,
            trans_func=lasagne.nonlinearities.rectify,
            out_func=lasagne.nonlinearities.linear,
            batch_size=batch_size,
            n_samples=self.snn_n_samples,
            prior_sd=self.prior_sd,
            use_reverse_kl_reg=self.use_reverse_kl_reg,
            reverse_kl_reg_factor=self.reverse_kl_reg_factor,
            second_order_update=self.second_order_update,
            learning_rate=self.unn_learning_rate,
            compression=self.compression,
            information_gain=self.information_gain,
            update_prior=(not self.use_replay_pool),
            update_likelihood_sd=self.update_likelihood_sd
        )

        logger.log(
            "Model built ({:.1f} sec).".format((time.time() - start_time)))

        if self.use_replay_pool:
            self.pool = SimpleReplayPool(
                max_pool_size=self.replay_pool_size,
                observation_shape=self.env.observation_space.shape,
                action_dim=act_dim
            )
        # ------------------------------------------------

        self.start_worker()
        self.init_opt()
        episode_rewards = []
        episode_lengths = []
        kl_factor = 1.0
        for itr in xrange(self.start_itr, self.n_itr):
            logger.push_prefix('itr #%d | ' % itr)

            paths = self.obtain_samples(itr)
            samples_data = self.process_samples(itr, paths)

            # Exploration code
            # ----------------
            if self.use_replay_pool:
                # Fill replay pool.
                logger.log("Fitting dynamics model using replay pool ...")
                for path in samples_data['paths']:
                    path_len = len(path['rewards'])
                    for i in xrange(path_len):
                        obs = path['observations'][i]
                        act = path['actions'][i]
                        rew = path['rewards'][i]
                        term = (i == path_len - 1)
                        self.pool.add_sample(obs, act, rew, term)

                # Now we train the dynamics model using the replay self.pool; only
                # if self.pool is large enough.
                if self.pool.size >= self.min_pool_size:
                    obs_mean, obs_std, act_mean, act_std = self.pool.mean_obs_act()
                    _inputss = []
                    _targetss = []
                    for _ in xrange(self.n_updates_per_sample / self.pool_batch_size):
                        batch = self.pool.random_batch(
                            self.pool_batch_size)
                        obs = (batch['observations'] - obs_mean) / \
                            (obs_std + 1e-8)
                        next_obs = (
                            batch['next_observations'] - obs_mean) / (obs_std + 1e-8)
                        act = (batch['actions'] - act_mean) / \
                            (act_std + 1e-8)
                        _inputs = np.hstack(
                            [obs, act])
                        _targets = next_obs
                        _inputss.append(_inputs)
                        _targetss.append(_targets)

                    old_acc = 0.
                    for _inputs, _targets in zip(_inputss, _targetss):
                        _out = self.bnn.pred_fn(_inputs)
                        old_acc += np.mean(np.square(_out - _targets))
                    old_acc /= len(_inputss)

                    for _inputs, _targets in zip(_inputss, _targetss):
                        self.bnn.train_fn(_inputs, _targets, kl_factor)
                        # DEBUG
                        # -----
#                         print(self.bnn.eval_loss(_inputs, _targets))
#                         print(self.bnn.fn_kl())
#                         print(self.bnn.fn_kl_from_prior())
#                         print(self.bnn.fn_dbg_nll(_inputs, _targets))
#                         print('---')
                        # -----

                    new_acc = 0.
                    for _inputs, _targets in zip(_inputss, _targetss):
                        _out = self.bnn.pred_fn(_inputs)
                        new_acc += np.mean(np.square(_out - _targets))
                    new_acc /= len(_inputss)

                    kl_factor *= self.replay_kl_schedule
                    logger.record_tabular('KLFactor', kl_factor)

                    logger.record_tabular(
                        'DynModelSqLossBefore', old_acc)
                    logger.record_tabular(
                        'DynModelSqLossAfter', new_acc)
            else:
                # Here we should take the current batch of samples and shuffle
                # them for i.d.d. purposes.
                logger.log(
                    "Fitting dynamics model to current sample batch ...")
                list_obs, list_obs_nxt, list_act = [], [], []
                for path in samples_data['paths']:
                    len_path = len(path['observations'])
                    for i in xrange(len_path - 1):
                        list_obs.append(path['observations'][i])
                        list_obs_nxt.append(
                            path['observations'][i + 1])
                        list_act.append(path['actions'][i])

                # Stack into input and target set.
                X_train = np.hstack((list_obs, list_act))
                T_train = np.asarray(list_obs_nxt)

                old_acc, new_acc = 0., 0.
                for batch in iterate_minibatches(X_train, T_train, self.pool_batch_size, shuffle=False):
                    _out = self.bnn.pred_fn(batch[0])
                    old_acc += np.mean(np.square(_out - batch[1]))
                old_acc /= n_batches

                # Save old parameters as new prior.
                self.bnn.save_old_params()

                # Num of runs needed to get to n_updates_per_sample
                for _ in xrange(n_iterations):
                    # Num batches to traverse.
                    for batch in iterate_minibatches(X_train, T_train, self.pool_batch_size, shuffle=True):
                        # Don't use kl_factor when using no replay pool.
                        self.bnn.train_fn(batch[0], batch[1], 1.0)

                        # DEBUG
                        # -----
#                         print(self.bnn.eval_loss(batch[0], batch[1]))
#                         print(self.bnn.fn_kl())
#                         print(self.bnn.fn_dbg_nll(batch[0], batch[1]))
#                         print('---')
                        # -----

                # DEBUG
                # -----
#                 loss = 0.
#                 kl_div = 0.
#                 nll = 0.
#                 count = 0
#                 for batch in iterate_minibatches(X_train, T_train, self.pool_batch_size, shuffle=True):
#                     loss += self.bnn.eval_loss(batch[0], batch[1])
#                     kl_div += self.bnn.fn_kl()
#                     nll += self.bnn.fn_dbg_nll(batch[0], batch[1])
#                     count += 1
#                 print(loss / count, nll / count, kl_div / count)
                # -----

                for batch in iterate_minibatches(X_train, T_train, self.pool_batch_size, shuffle=False):
                    _out = self.bnn.pred_fn(batch[0])
                    new_acc += np.mean(np.square(_out - batch[1]))
                new_acc /= n_batches

                logger.record_tabular(
                    'DynModelSqErrBefore', old_acc)
                logger.record_tabular(
                    'DynModelSqErrAfter', new_acc)

            # ----------------

            self.env.log_diagnostics(paths)
            self.policy.log_diagnostics(paths)
            self.baseline.log_diagnostics(paths)
            self.optimize_policy(itr, samples_data)
            logger.log("Saving snapshot ...")
            params = self.get_itr_snapshot(itr, samples_data)
            paths = samples_data["paths"]
            if self.store_paths:
                params["paths"] = paths
            episode_rewards.extend(sum(p["rewards"]) for p in paths)
            episode_lengths.extend(len(p["rewards"]) for p in paths)
            params["episode_rewards"] = np.array(episode_rewards)
            params["episode_lengths"] = np.array(episode_lengths)
            params["algo"] = self
            logger.save_itr_params(itr, params)
            logger.log("Saved.")
            logger.dump_tabular(with_prefix=False)
            logger.pop_prefix()
            if self.plot:
                self.update_plot()
                if self.pause_for_plot:
                    raw_input("Plotting evaluation run: Press Enter to "
                              "continue...")

        # Training complete: terminate environment.
        self.shutdown_worker()
        self.env.terminate()
        self.policy.terminate()

    def init_opt(self):
        """
        Initialize the optimization procedure. If using theano / cgt, this may
        include declaring all the variables and compiling functions
        """
        raise NotImplementedError

    def get_itr_snapshot(self, itr, samples_data):
        """
        Returns all the data that should be saved in the snapshot for this
        iteration.
        """
        raise NotImplementedError

    def optimize_policy(self, itr, samples_data):
        raise NotImplementedError

    def update_plot(self):
        if self.plot:
            plotter.update_plot(self.policy, self.max_path_length)

    def obtain_samples(self, itr):
        cur_params = self.policy.get_param_values()
        cur_dynamics_params = self.bnn.get_param_values()

        reward_mean = None
        reward_std = None
        if self.normalize_reward:
            # Compute running mean/std.
            reward_mean = np.mean(np.asarray(self._reward_mean))
            reward_std = np.mean(np.asarray(self._reward_std))

        # Mean/std obs/act based on replay pool.
        if self.use_replay_pool:
            obs_mean, obs_std, act_mean, act_std = self.pool.mean_obs_act()
        else:
            obs_mean, obs_std, act_mean, act_std = 0, 1, 0, 1

        paths = parallel_sampler.sample_paths(
            policy_params=cur_params,
            dynamics_params=cur_dynamics_params,
            max_samples=self.batch_size,
            max_path_length=self.max_path_length,
            itr=itr,
            normalize_reward=self.normalize_reward,
            reward_mean=reward_mean,
            reward_std=reward_std,
            kl_batch_size=self.kl_batch_size,
            n_itr_update=self.n_itr_update,
            use_replay_pool=self.use_replay_pool,
            obs_mean=obs_mean,
            obs_std=obs_std,
            act_mean=act_mean,
            act_std=act_std,
            second_order_update=self.second_order_update
        )

        # DEBUG
        # -----
        if itr > 0 and False:
            for path in paths:
                if 'all_r' in path.keys():
                    r = path['all_r']
                    kls = path['all_kls']
                    print(r, kls)
                    import matplotlib.pyplot as plt
                    plt.plot(
                        r, kls, '-', color=(1.0, 0, 0, 0.5))
                    plt.draw()
                    plt.show()
        # -----

        if self.whole_paths:
            return paths
        else:
            paths_truncated = parallel_sampler.truncate_paths(
                paths, self.batch_size)
            return paths_truncated

    def process_samples(self, itr, paths):

        if self.normalize_reward:
            # Update reward mean/std Q.
            rewards = []
            for i in xrange(len(paths)):
                rewards.append(paths[i]['rewards'])
            rewards_flat = np.hstack(rewards)
            self._reward_mean.append(np.mean(rewards_flat))
            self._reward_std.append(np.std(rewards_flat))

            # Normalize rewards.
            reward_mean = np.mean(np.asarray(self._reward_mean))
            reward_std = np.mean(np.asarray(self._reward_std))
            for i in xrange(len(paths)):
                paths[i]['rewards'] = (
                    paths[i]['rewards'] - reward_mean) / (reward_std + 1e-8)

        if itr > 0:
            kls = []
            for i in xrange(len(paths)):
                kls.append(paths[i]['KL'])

            kls_flat = np.hstack(kls)

            logger.record_tabular('BNN_MeanKL', np.mean(kls_flat))
            logger.record_tabular('BNN_StdKL', np.std(kls_flat))
            logger.record_tabular('BNN_MinKL', np.min(kls_flat))
            logger.record_tabular('BNN_MaxKL', np.max(kls_flat))
            logger.record_tabular('BNN_MedianKL', np.median(kls_flat))
            logger.record_tabular('BNN_25percKL', np.percentile(kls_flat, 25))
            logger.record_tabular('BNN_75percKL', np.percentile(kls_flat, 75))
            logger.record_tabular('BNN_90percKL', np.percentile(kls_flat, 90))

            # Transform intrinsic rewards.
            if self.surprise_transform == 'log(1+surprise)':
                # Transform surprise into (positive) log space.
                for i in xrange(len(paths)):
                    kls[i] = np.log(1 + kls[i])
            elif self.surprise_transform == 'cap90perc':
                perc90 = np.percentile(np.hstack(kls), 90)
                # Cap max KL for stabilization.
                for i in xrange(len(paths)):
                    kls[i] = np.minimum(kls[i], perc90)
            elif self.surprise_transform == 'cap1000':
                # Cap max KL for stabilization.
                for i in xrange(len(paths)):
                    kls[i] = np.minimum(kls[i], 1000)

            kls_flat = np.hstack(kls)

            logger.record_tabular('BNN_MeanKL_transf', np.mean(kls_flat))
            logger.record_tabular('BNN_StdKL_transf', np.std(kls_flat))
            logger.record_tabular('BNN_MinKL_transf', np.min(kls_flat))
            logger.record_tabular('BNN_MaxKL_transf', np.max(kls_flat))
            logger.record_tabular('BNN_MedianKL_transf', np.median(kls_flat))
            logger.record_tabular(
                'BNN_25percKL_transf', np.percentile(kls_flat, 25))
            logger.record_tabular(
                'BNN_75percKL_transf', np.percentile(kls_flat, 75))
            logger.record_tabular(
                'BNN_90percKL_transf', np.percentile(kls_flat, 90))

            # Normalize intrinsic rewards.
            if self.use_kl_ratio:
                if self.use_kl_ratio_q:
                    # Update kl Q
                    self.kl_previous.append(np.median(np.hstack(kls)))
                    previous_mean_kl = np.mean(np.asarray(self.kl_previous))
                    for i in xrange(len(kls)):
                        kls[i] = kls[i] / previous_mean_kl
                else:
                    median_KL_current_batch = np.median(np.hstack(kls))
                    for i in xrange(len(kls)):
                        kls[i] = kls[i] / median_KL_current_batch

            kls_flat = np.hstack(kls)

            logger.record_tabular('BNN_MeanKL_norm', np.mean(kls_flat))
            logger.record_tabular('BNN_StdKL_norm', np.std(kls_flat))
            logger.record_tabular('BNN_MinKL_norm', np.min(kls_flat))
            logger.record_tabular('BNN_MaxKL_norm', np.max(kls_flat))
            logger.record_tabular('BNN_MedianKL_norm', np.median(kls_flat))
            logger.record_tabular(
                'BNN_25percKL_norm', np.percentile(kls_flat, 25))
            logger.record_tabular(
                'BNN_75percKL_norm', np.percentile(kls_flat, 75))
            logger.record_tabular(
                'BNN_90percKL_norm', np.percentile(kls_flat, 90))

            # Add KL as intrinsic reward to external reward
            for i in xrange(len(paths)):
                paths[i]['rewards'] = paths[i]['rewards'] + self.eta * kls[i]

        else:
            logger.record_tabular('BNN_MeanKL', 0.)
            logger.record_tabular('BNN_StdKL', 0.)
            logger.record_tabular('BNN_MinKL', 0.)
            logger.record_tabular('BNN_MaxKL', 0.)
            logger.record_tabular('BNN_MedianKL', 0.)
            logger.record_tabular('BNN_25percKL', 0.)
            logger.record_tabular('BNN_75percKL', 0.)
            logger.record_tabular('BNN_90percKL', 0.)

            logger.record_tabular('BNN_MeanKL_transf', 0.)
            logger.record_tabular('BNN_StdKL_transf', 0.)
            logger.record_tabular('BNN_MinKL_transf', 0.)
            logger.record_tabular('BNN_MaxKL_transf', 0.)
            logger.record_tabular('BNN_MedianKL_transf', 0.)
            logger.record_tabular('BNN_25percKL_transf', 0.)
            logger.record_tabular('BNN_75percKL_transf', 0.)
            logger.record_tabular('BNN_90percKL_transf', 0.)

            logger.record_tabular('BNN_MeanKL_norm', 0.)
            logger.record_tabular('BNN_StdKL_norm', 0.)
            logger.record_tabular('BNN_MinKL_norm', 0.)
            logger.record_tabular('BNN_MaxKL_norm', 0.)
            logger.record_tabular('BNN_MedianKL_norm', 0.)
            logger.record_tabular('BNN_25percKL_norm', 0.)
            logger.record_tabular('BNN_75percKL_norm', 0.)
            logger.record_tabular('BNN_90percKL_norm', 0.)

        baselines = []
        returns = []
        for path in paths:
            path_baselines = np.append(self.baseline.predict(path), 0)
            deltas = path["rewards"] + \
                self.discount * path_baselines[1:] - \
                path_baselines[:-1]
            path["advantages"] = special.discount_cumsum(
                deltas, self.discount * self.gae_lambda)
            path["returns"] = special.discount_cumsum(
                path["rewards_orig"], self.discount)
            baselines.append(path_baselines[:-1])
            returns.append(path["returns"])

        if not self.policy.recurrent:
            observations = tensor_utils.concat_tensor_list(
                [path["observations"] for path in paths])
            actions = tensor_utils.concat_tensor_list(
                [path["actions"] for path in paths])
            rewards = tensor_utils.concat_tensor_list(
                [path["rewards"] for path in paths])
            advantages = tensor_utils.concat_tensor_list(
                [path["advantages"] for path in paths])
            env_infos = tensor_utils.concat_tensor_dict_list(
                [path["env_infos"] for path in paths])
            agent_infos = tensor_utils.concat_tensor_dict_list(
                [path["agent_infos"] for path in paths])

            if self.center_adv:
                advantages = util.center_advantages(advantages)

            if self.positive_adv:
                advantages = util.shift_advantages_to_positive(advantages)

            average_discounted_return = \
                np.mean([path["returns"][0] for path in paths])

            undiscounted_returns = [
                sum(path["rewards_orig"]) for path in paths]

            ent = np.mean(self.policy.distribution.entropy(agent_infos))

            ev = special.explained_variance_1d(
                np.concatenate(baselines),
                np.concatenate(returns)
            )

            samples_data = dict(
                observations=observations,
                actions=actions,
                rewards=rewards,
                advantages=advantages,
                env_infos=env_infos,
                agent_infos=agent_infos,
                paths=paths,
            )
        else:
            max_path_length = max([len(path["advantages"]) for path in paths])

            # make all paths the same length (pad extra advantages with 0)
            obs = [path["observations"] for path in paths]
            obs = np.array(
                [tensor_utils.pad_tensor(ob, max_path_length) for ob in obs])

            if self.center_adv:
                raw_adv = np.concatenate(
                    [path["advantages"] for path in paths])
                adv_mean = np.mean(raw_adv)
                adv_std = np.std(raw_adv) + 1e-8
                adv = [
                    (path["advantages"] - adv_mean) / adv_std for path in paths]
            else:
                adv = [path["advantages"] for path in paths]

            adv = np.array(
                [tensor_utils.pad_tensor(a, max_path_length) for a in adv])

            actions = [path["actions"] for path in paths]
            actions = np.array(
                [tensor_utils.pad_tensor(a, max_path_length) for a in actions])

            rewards = [path["rewards"] for path in paths]
            rewards = np.array(
                [tensor_utils.pad_tensor(r, max_path_length) for r in rewards])

            agent_infos = [path["agent_infos"] for path in paths]
            agent_infos = tensor_utils.stack_tensor_dict_list(
                [tensor_utils.pad_tensor_dict(
                    p, max_path_length) for p in agent_infos]
            )

            env_infos = [path["env_infos"] for path in paths]
            env_infos = tensor_utils.stack_tensor_dict_list(
                [tensor_utils.pad_tensor_dict(
                    p, max_path_length) for p in env_infos]
            )

            valids = [np.ones_like(path["returns"]) for path in paths]
            valids = np.array(
                [tensor_utils.pad_tensor(v, max_path_length) for v in valids])

            average_discounted_return = \
                np.mean([path["returns"][0] for path in paths])

            undiscounted_returns = [
                sum(path["rewards_orig"]) for path in paths]

            ent = np.mean(self.policy.distribution.entropy(agent_infos))

            ev = special.explained_variance_1d(
                np.concatenate(baselines),
                np.concatenate(returns)
            )

            samples_data = dict(
                observations=obs,
                actions=actions,
                advantages=adv,
                rewards=rewards,
                valids=valids,
                agent_infos=agent_infos,
                env_infos=env_infos,
                paths=paths,
            )

        logger.log("fitting baseline...")
        self.baseline.fit(paths)
        logger.log("fitted")

        average_reward = np.mean(
            [np.mean(path["rewards_orig"]) for path in paths])
        min_reward = np.min(
            [np.min(path["rewards_orig"]) for path in paths])
        max_reward = np.min(
            [np.min(path["rewards_orig"]) for path in paths])

        logger.record_tabular('Iteration', itr)
        logger.record_tabular('AverageReward', average_reward)
        logger.record_tabular('MinReward', min_reward)
        logger.record_tabular('MaxReward', max_reward)
        logger.record_tabular('AverageDiscountedReturn',
                              average_discounted_return)
        logger.record_tabular('AverageReturn', np.mean(undiscounted_returns))
        logger.record_tabular('ExplainedVariance', ev)
        logger.record_tabular('NumTrajs', len(paths))
        logger.record_tabular('Entropy', ent)
        logger.record_tabular('Perplexity', np.exp(ent))
        logger.record_tabular('StdReturn', np.std(undiscounted_returns))
        logger.record_tabular('MaxReturn', np.max(undiscounted_returns))
        logger.record_tabular('MinReturn', np.min(undiscounted_returns))
        logger.record_tabular('Expl_eta', self.eta)
        logger.record_tabular('LikelihoodStd', self.bnn.likelihood_sd.eval())

        return samples_data