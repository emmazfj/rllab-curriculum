from rllab.envs.base import Step
from rllab.misc.overrides import overrides
from rllab.envs.mujoco.mujoco_env import MujocoEnv
import numpy as np
from rllab.core.serializable import Serializable
from rllab.misc import logger
from rllab.misc import autoargs

import matplotlib as mpl
mpl.use('Agg')
import matplotlib.pyplot as plt
import os.path as osp


from rllab import spaces
BIG = 1e6

class SwimmerEnv(MujocoEnv, Serializable):
    FILE = 'swimmer.xml'

    @autoargs.arg('ctrl_cost_coeff', type=float,
                  help='cost coefficient for controls')
    def __init__(
            self,
            ctrl_cost_coeff=1e-2,
            *args, **kwargs):
        self.ctrl_cost_coeff = ctrl_cost_coeff
        super(SwimmerEnv, self).__init__(*args, **kwargs)
        Serializable.quick_init(self, locals())

    def get_current_obs(self):
        return np.concatenate([
            self.model.data.qpos.flat,
            self.model.data.qvel.flat,
            self.get_body_com("torso").flat,
        ]).reshape(-1)

## hack that I will have to remove!!!
    @property
    def robot_observation_space(self):
        shp = self.get_current_obs().shape
        ub = BIG * np.ones(shp)
        return spaces.Box(ub * -1, ub)

    @property
    def maze_observation_space(self):
        ub = BIG * np.array(())
        return spaces.Box(ub, ub)


    def step(self, action):
        self.forward_dynamics(action)
        next_obs = self.get_current_obs()
        lb, ub = self.action_bounds
        scaling = (ub - lb) * 0.5
        ctrl_cost = 0.5 * self.ctrl_cost_coeff * np.sum(
            np.square(action / scaling))
        forward_reward = np.linalg.norm(self.get_body_comvel("torso"))  # swimmer has no problem of jumping reward
        reward = forward_reward - ctrl_cost
        done = False
        # print 'obs x: {}, obs y: {}'.format(next_obs[-3],next_obs[-2])
        return Step(next_obs, reward, done)

    @overrides
    def log_diagnostics(self, paths):
        # instead of just path["obs"][-1][-3] we will look at the distance to origin
        progs = [
            np.linalg.norm(path["observations"][-1][-3:-1] - path["observations"][0][-3:-1])
            # gives (x,y) coord -not last z
            for path in paths
            ]
        logger.record_tabular('AverageForwardProgress', np.mean(progs))
        logger.record_tabular('MaxForwardProgress', np.max(progs))
        logger.record_tabular('MinForwardProgress', np.min(progs))
        logger.record_tabular('StdForwardProgress', np.std(progs))

        # now we will grid the space and check how much of it the policy is covering
        furthest = np.ceil(np.abs(np.max(np.concatenate([path["observations"][:, -3:-1] for path in paths]))))
        print 'THE FUTHEST IT WENT COMPONENT-WISE IS', furthest
        furthest = max(furthest, 5)
        mesh_density = 50
        c_grid = int(furthest * 50 * 2)

        if 'agent_infos' in paths[0].keys() and 'latents' in paths[0]['agent_infos'].keys():
            dict_visit = {}
            # keep track of the overlap
            overlap = 0
            for path in paths:
                lat = str(path['agent_infos']['latents'][0])
                if lat not in dict_visit.keys():
                    dict_visit[lat] = np.zeros((c_grid + 1, c_grid + 1))
                com_x = np.clip(np.ceil(((np.array(path['observations'][:, -3]) + furthest) * 50)).astype(int), 0,
                                c_grid)
                com_y = np.clip(np.ceil(((np.array(path['observations'][:, -2]) + furthest) * 50)).astype(int), 0,
                                c_grid)
                coms = zip(com_x, com_y)
                for com in coms:
                    dict_visit[lat][com] += 1
            num_latents = len(dict_visit.keys())
            num_colors = num_latents + 2  # +2 for the 0 and Repetitions
            cmap = plt.get_cmap('nipy_spectral', num_colors)
            visitation_by_lat = np.zeros((c_grid + 1, c_grid + 1))
            for i, visit in enumerate(dict_visit.itervalues()):
                lat_visit = np.where(visit == 0, visit, i + 1)  # transform the map into 0 or i+1
                visitation_by_lat += lat_visit
                overlap += np.sum(np.where(visitation_by_lat > lat_visit))  # add the overlaps of this latent
                visitation_by_lat = np.where(visitation_by_lat <= i + 1, visitation_by_lat,
                                             num_colors - 1)  # mark overlaps
            x = np.arange(c_grid + 1) / 50. - furthest
            y = np.arange(c_grid + 1) / 50. - furthest

            plt.figure()
            map_plot = plt.pcolormesh(x, y, visitation_by_lat, cmap=cmap, vmin=0.1, vmax=num_latents + 1)
            color_len = (num_colors - 1.) / num_colors
            ticks = np.arange(color_len / 2., num_colors - 1, color_len)
            cbar = plt.colorbar(map_plot, ticks=ticks)
            latent_tick_labels = ['latent: ' + l for l in dict_visit.keys()]
            cbar.ax.set_yticklabels(['No visitation'] + latent_tick_labels + ['Repetitions'])  # horizontal colorbar

            # still log the total visitation and the overlap
            visitation = reduce(np.add, [visit for visit in dict_visit.itervalues()])
        else:
            visitation = np.zeros((c_grid + 1, c_grid + 1))
            for path in paths:
                com_x = np.clip(np.ceil(((np.array(path['observations'][:, -3]) + furthest) * 50)).astype(int), 0,
                                c_grid)
                com_y = np.clip(np.ceil(((np.array(path['observations'][:, -2]) + furthest) * 50)).astype(int), 0,
                                c_grid)
                coms = zip(com_x, com_y)
                for com in coms:
                    visitation[com] += 1
            x = np.arange(c_grid + 1) / 50. - furthest
            y = np.arange(c_grid + 1) / 50. - furthest

            plt.figure()
            plt.pcolormesh(x, y, visitation, vmax=50)
            overlap = np.sum(np.where(visitation > 1, visitation, 0))  # sum of all visitations larger than 1
        plt.xlim([x[0], x[-1]])
        plt.ylim([y[0], y[-1]])

        log_dir = logger.get_snapshot_dir()
        exp_name = log_dir.split('/')[-1]
        plt.title('visitation: ' + exp_name)

        plt.savefig(osp.join(log_dir, 'visitation.png'))

        total_visitation = np.count_nonzero(visitation)
        logger.record_tabular('VisitationTotal', total_visitation)
        logger.record_tabular('VisitationOverlap', overlap)