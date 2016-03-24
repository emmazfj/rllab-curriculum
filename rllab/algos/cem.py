from rllab.algos.base import RLAlgorithm

import theano.tensor as TT
import numpy as np

from rllab.misc import autoargs
from rllab.misc.special import discount_cumsum
from rllab.sampler import parallel_sampler
from rllab.sampler.parallel_sampler import pool_map, G
from rllab.sampler.utils import rollout
import rllab.misc.logger as logger
import rllab.plotter as plotter

def sample_return(mdp, policy, params, max_path_length, discount):
    # mdp, policy, params, max_path_length, discount = args
    # of course we make the strong assumption that there is no race condition
    policy.set_param_values(params)
    path = rollout(
        mdp,
        policy,
        max_path_length,
    )
    path["returns"] = discount_cumsum(path["rewards"], discount)
    undiscounted_return = sum(path["rewards"])
    return dict(
        returns=path['returns'],
        undiscounted_return=undiscounted_return,
    )


class CEM(RLAlgorithm):
    @autoargs.arg("n_itr", type=int,
                  help="Number of iterations.")
    @autoargs.arg("max_path_length", type=int,
                  help="Maximum length of a single rollout.")
    @autoargs.arg("discount", type=float,
                  help="Discount.")
    @autoargs.arg("whole_paths", type=bool,
                  help="Make sure that the samples contain whole "
                       "trajectories, even if the actual batch size is "
                       "slightly larger than the specified batch_size.")
    @autoargs.arg("init_std", type=float,
                  help="Initial std for param distribution")
    @autoargs.arg("extra_std", type=float,
                  help="Decaying std added to param distribution at each iteration")
    @autoargs.arg("extra_decay_time", type=int,
                  help="Iterations that it takes to decay extra std")
    @autoargs.arg("n_samples", type=int,
                  help="# of samples from param distribution")
    @autoargs.arg("batch_size", type=int,
                  help="# of samples from trajs from param distribution, when"
                       "this is set, n_samples is ignored")
    @autoargs.arg("best_frac", type=float,
                  help="Best fraction of the sampled params")
    @autoargs.arg("plot", type=bool,
                  help="Plot evaluation run after each iteration")
    def __init__(
            self,
            n_itr=500,
            max_path_length=500,
            discount=0.99,
            whole_paths=True,
            init_std=1.,
            n_samples=100,
            batch_size=None,
            best_frac=0.05,
            extra_std=1.,
            extra_decay_time=100,
            plot=False,
            **kwargs
    ):
        super(CEM, self).__init__(**kwargs)
        self.batch_size = batch_size
        self.plot = plot
        self.extra_decay_time = extra_decay_time
        self.extra_std = extra_std
        self.best_frac = best_frac
        self.n_samples = n_samples
        self.init_std = init_std
        self.whole_paths = whole_paths
        assert whole_paths, "Cannot handle otherwise"
        self.discount = discount
        self.max_path_length = max_path_length
        self.n_itr = n_itr

    def train(self, mdp, policy, **kwargs):
        parallel_sampler.populate_task(mdp, policy)
        if self.plot:
            plotter.init_plot(mdp, policy)

        cur_std = self.init_std
        cur_mean = policy.get_param_values()
        K = cur_mean.size
        n_best = int(self.n_samples * self.best_frac)

        for itr in range(self.n_itr):
            # sample around the current distribution
            extra_var_mult = max(1.0 - itr / self.extra_decay_time, 0)
            sample_std = np.sqrt(np.square(cur_std) + np.square(self.extra_std) * extra_var_mult)
            if self.batch_size is None:
                xs = np.random.randn(self.n_samples, K) * sample_std.reshape(1, -1) + cur_mean.reshape(1, -1)
                infos = (pool_map(sample_return, [(x, self.max_path_length, self.discount) for x in xs]))
            else:
                cum_len = 0
                infos = []
                xss = []
                done = False
                while not done:
                    sbs = G.n_parallel * 2
                    xs = np.random.randn(sbs, K) * sample_std.reshape(1, -1) + cur_mean.reshape(1, -1)
                    xss.append(xs)
                    sinfos = pool_map(sample_return, [(x, self.max_path_length, self.discount) for x in xs])
                    for info in sinfos:
                        infos.append(info)
                        cum_len += len(info['returns'])
                        if cum_len >= self.batch_size:
                            xs = np.concatenate(xss)
                            done = True
                            break

            
            fs = np.array([info['returns'][0] for info in infos])
            print(xs.shape, fs.shape)
            best_inds = (-fs).argsort()[:n_best]
            best_xs = xs[best_inds]
            cur_mean = best_xs.mean(axis=0)
            cur_std = best_xs.std(axis=0)
            best_x = best_xs[0]
            logger.push_prefix('itr #%d | ' % itr)
            logger.record_tabular('Iteration', itr)
            logger.record_tabular('CurStdMean', np.mean(cur_std))
            undiscounted_returns = np.array([info['undiscounted_return'] for info in infos])
            logger.record_tabular('AverageReturn',
                                  np.mean(undiscounted_returns))
            logger.record_tabular('StdReturn',
                                  np.mean(undiscounted_returns))
            logger.record_tabular('MaxReturn',
                                  np.max(undiscounted_returns))
            logger.record_tabular('MinReturn',
                                  np.min(undiscounted_returns))
            logger.record_tabular('AverageDiscountedReturn',
                                  np.mean(fs))
            logger.record_tabular('AvgTrajLen',
                                  np.mean([len(info['returns']) for info in infos]))
            logger.record_tabular('NumTrajs',
                                  len(infos))
            policy.set_param_values(best_x)
            logger.save_itr_params(itr, dict(
                itr=itr,
                policy=policy,
                mdp=mdp,
                cur_mean=cur_mean,
                cur_std=cur_std,
            ))
            logger.dump_tabular(with_prefix=False)
            if self.plot:
                plotter.update_plot(policy, self.max_path_length)