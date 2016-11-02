from __future__ import print_function
from sandbox.dave.rllab.envs.mujoco.mujoco_env import MujocoEnv
import numpy as np
from sandbox.dave.pr2.action_limiter import FixedActionLimiter
from scipy.misc import imsave
from sandbox.dave.rllab.mujoco_py.mjviewer_openai import MjViewer
from rllab.core.serializable import Serializable
from rllab.envs.base import Step
from rllab.misc.overrides import overrides
from rllab.misc import logger
from scipy.misc import imresize
import time

class Pr2EnvLego(MujocoEnv, Serializable):

    FILE = 'pr2_legofree.xml' #'pr2_1arm.xml'

    def __init__(
            self,
            goal_generator=None,
            lego_generator=None,
            action_penalty_weight=0, #originally was 0.001 #there is one with 0.0005
            distance_thresh=0.01,  # 1 cm
            model='pr2_legofree.xml', #'pr2_1arm.xml',
            max_action=float("inf"),
            allow_random_restarts=True,   #same position: True
            allow_random_vel_restarts=True,
            qvel_init_std=1, #0.01,
            pos_normal_sample=False,
            pos_normal_sample_std=0.01,
            action_limiter=FixedActionLimiter(),
            use_running_average_failure_rate=False,
            failure_rate_gamma=0.9,
            mean_failure_rate_init=1.0,
            offset=np.zeros(3),
            use_vision=False,
            use_depth=False,
            *args, **kwargs):

        self.action_penalty_weight = action_penalty_weight
        self.distance_thresh = distance_thresh
        self.counter = 1
        self._goal_generator = goal_generator
        self._lego_generator = lego_generator
        self._action_limiter = action_limiter
        self.allow_random_restarts = allow_random_restarts
        self.allow_random_vel_restarts = allow_random_vel_restarts
        self.goal_dims = 3
        self.first_time = True
        self.goal = None
        self.lego = None
        if model not in [None, 0]:
            self.set_model(model)
        self.action_limit = max_action
        self.max_action_limit = 3
        self.min_action_limit = 0.1
        self.qvel_init_std = qvel_init_std
        self.pos_normal_sample = pos_normal_sample
        self.pos_normal_sample_std=pos_normal_sample_std
        self.mean_failure_rate = mean_failure_rate_init
        self.failure_rate_gamma = failure_rate_gamma
        self.use_running_average_failure_rate = use_running_average_failure_rate
        self.offset = offset
        self.distance_tip_lego_penalty_weight = 0.6 #0.4 #1  #0.1  #0.3
        self.angle_penalty_weight = 0.2 #0.2 #0.4 #0.5 #1 #0.05
        self.use_vision = use_vision
        self.use_depth = use_depth
        self.discount = 0.95

        super(Pr2EnvLego, self).__init__(*args, **kwargs)
        Serializable.quick_init(self, locals())

    def set_model(self, model):
        self.__class__.FILE = model

    def get_current_obs(self):
        #assert(np.array_equal(self.model.data.qpos[-3:], self.goal[:,None]))

        vec_to_goal = self.get_vec_to_goal()
        dim = self.model.data.qpos.shape[0]

        if self.use_depth:
            idxpos = list(range(7)) + list(range(14, dim))  # TODO: Hacky
            idxvel = list(range(7)) + list(range(14, dim - 3 - 1))
            return  np.concatenate([
                self.model.data.qpos.flat[idxpos],
                # self.model.data.qpos.flat[:-3], # We do not need to explicitly include the goal
                #                                 # since we already have the vec to the goal.
                self.model.data.qvel.flat[idxvel],  # Do not include the velocity of the target (should be 0).
                vec_to_goal,
                self.get_tip_position(),
                self.get_vec_tip_to_lego(),
                # self.viewer_bot.get_depth_map(),
            ]).reshape(-1)
        else:
            # depth = self.viewer_bot.get_depth_map()
            # depth = depth.astype(np.uint8)
            # depth = imresize(depth, (227, 227))
            # depth = depth.astype(np.float32).transpose([2, 0, 1])
            idxpos = list(range(7)) + list(range(dim - 3, dim))  # TODO: Hacky
            idxvel = list(range(7))
            return np.concatenate([
                self.model.data.qpos.flat[idxpos],
                # self.model.data.qpos.flat[:-3], # We do not need to explicitly include the goal
                #                                 # since we already have the vec to the goal.
                self.model.data.qvel.flat[idxvel],  # Do not include the velocity of the target (should be 0).
                self.get_tip_position(),
                self.get_vec_tip_to_lego(),
                np.reshape(depth, -1),
            ]).reshape(-1)



        # return  np.concatenate([
        #     self.model.data.qpos.flat[idxpos],
        #     # self.model.data.qpos.flat[:-3], # We do not need to explicitly include the goal
        #     #                                 # since we already have the vec to the goal.
        #     self.model.data.qvel.flat[idxvel],  # Do not include the velocity of the target (should be 0).
        #     vec_to_goal,
        #     #self.get_tip_position(),
        #     self.get_tip_position(),
        #     self.get_vec_tip_to_lego(),
        #     [self.get_cos_vecs()],
        #     # Add the action limit so that we learn a range of policies.
        #     #[self.action_limit]
        #     #np.clip(self.model.data.cfrc_ext, -1, 1).flat,
        # ]).reshape(-1)
        #







        # return np.concatenate([
        #     self.model.data.qpos.flat,
        #     # self.model.data.qpos.flat[:-3], # We do not need to explicitly include the goal
        #     #                                 # since we already have the vec to the goal.
        #     self.model.data.qvel.flat[:-3], # Do not include the velocity of the target (should be 0).
        #     #self.get_tip_position(),
        #     vec_to_goal,
        #     # Add the action limit so that we learn a range of policies.
        #     #[self.action_limit]
        #     #np.clip(self.model.data.cfrc_ext, -1, 1).flat,
        # ]).reshape(-1)

    def get_tip_position(self):
        #return self.get_body_com("l_gripper_r_finger_tip_link")
        return self.model.data.site_xpos[0]

    def get_lego_position(self):
        #Return the position of the lego block
        return self.model.data.site_xpos[-1]

    def get_vec_to_goal(self):
        lego_position = self.get_lego_position()
        # Compute the distance to the goal
        vec_to_goal = lego_position - (self.goal + self.offset)
        return vec_to_goal

    def get_vec_tip_to_lego(self):
        tip_position = self.get_tip_position()
        lego_position = self.get_lego_position()
        vec_tip_to_lego = lego_position - tip_position
        return vec_tip_to_lego

    def get_cos_vecs(self):
        vec_tip_to_lego = self.get_vec_tip_to_lego()
        vec_to_goal = self.get_vec_to_goal()
        return np.dot(vec_to_goal[:2], vec_tip_to_lego[:2]) / (
            np.linalg.norm(vec_to_goal[:2]) * np.linalg.norm(vec_tip_to_lego[:2]))

    def step(self, action):
        #action /= 10

        #image, width, height = self.viewer.get_image()

        # Limit actions to the specified range.
        action_limit = self.action_limit * self.action_space.ones()
        action = np.maximum(action, -action_limit)
        action = np.minimum(action, action_limit)

        vec_tip_to_lego = self.get_vec_tip_to_lego()
        distance_tip_to_lego_previous = np.linalg.norm(vec_tip_to_lego)
        cos_angle_previous = self.get_cos_vecs()

        # Simulate this action and get the resulting state.
        self.forward_dynamics(action)

        # Compute the magnitude of the position error
        vec_to_goal = self.get_vec_to_goal()
        distance_to_goal = np.linalg.norm(vec_to_goal)
        distance_tip_to_lego = np.linalg.norm(vec_tip_to_lego)

        # Penalize the robot for being far from the goal and for having the arm far from the lego.
        reward_dist = - distance_to_goal
        reward_tip = - self.distance_tip_lego_penalty_weight * distance_tip_to_lego

        cos_angle = self.get_cos_vecs()
        reward_angle = - self.angle_penalty_weight * cos_angle
        # Penalize the robot for large actions.
        reward_ctrl = - self.action_penalty_weight * np.square(action).sum()
        reward = reward_dist + reward_tip + reward_ctrl + reward_angle
        state = self._state
        notdone = np.isfinite(state).all()
        done = not notdone
        ob = self.get_current_obs()
        # Viewer
        if self.use_vision:
            self.viewer.loop_once()
        if self.use_depth:
            self.viewer_bot.loop_once()

        img = self.viewer_bot.get_depth_map()
        img = img.astype(np.ubyte)
        imsave('data/local/imgs/depth' + str(time.clock()) + '.png', img)
        (img, h, w) = self.viewer.get_image()
        rect = self.viewer_bot.get_rect()
        img = np.fromstring(img, dtype='uint8').reshape(h, w, 3)[::-1, :, :]
        imsave('data/local/imgs/lego' + str(time.clock()) + '.png', img)
        # if self.use_depth:

        return Step(ob, float(reward), done, #not self.do_rand,
                    distance_to_goal=distance_to_goal,
                    distance_to_goal_x=vec_to_goal[0],
                    distance_to_goal_y=vec_to_goal[1],
                    distance_to_goal_z=vec_to_goal[2],
                    distance_tip_to_lego=distance_tip_to_lego,
                    reward_dist=reward_dist,
                    reward_tip=reward_tip,
                    reward_angle=reward_angle,
                    )

    def viewer_setup(self, is_bot=False):
        #self.viewer.cam.lookat[0] = self.model.stat.center[0]
        #self.viewer.cam.lookat[1] = self.model.stat.center[1]
        #self.viewer.cam.lookat[2] = self.model.stat.center[2]
        if not is_bot:
            self.viewer.cam.distance = self.model.stat.extent * 1.5
            self.viewer.cam.camid = -1
        else:
            self.viewer_bot.cam.camid = 0
        #self.viewer.cam.trackbodyid = -1   # 39
        #self.viewer.cam.elevation = 0
        #self.viewer.cam.azimuth = 0
        #self.viewer.cam.VR = 1


    @overrides
    def reset_mujoco(self):
        goal_dims = 3 # self.goal_dims
        lego_dims = 6

        if self.allow_random_restarts or self.first_time:
            if self.pos_normal_sample:
                # Sample a new random initial robot position from a normal distribution.
                # ini = np.zeros(self.init_qpos.shape)
                # ini[0:3] = np.reshape(np.array([0.5, 0, 6]), (3,1))
                qpos = self.init_qpos + np.random.normal(size=self.init_qpos.shape) * self.pos_normal_sample_std
            else:
                # Sample a new random initial robot position uniformly from the full joint limit range
                qpos = np.zeros(self.model.data.qpos.shape)
                for idx, jnt_range in enumerate(self.model.jnt_range):
                    qpos[idx] = np.random.uniform(jnt_range[0], jnt_range[1])
                # Make sure joints are within limits
            for idx, jnt_range in enumerate(self.model.jnt_range):
                if self.model.jnt_limited[idx] == 1:
                    qpos[idx] = max(jnt_range[0], qpos[idx])
                    qpos[idx] = min(jnt_range[1], qpos[idx])
                    if idx == 1 or idx == 2:
                        qpos[idx] = max(jnt_range[0], qpos[idx])
                        qpos[idx] = min((jnt_range[1]+jnt_range[0])/2, qpos[idx])

        else:
            # Use current position as new position.
            qpos_curr = self.model.data.qpos #[:-goal_dims]
            qpos = list(qpos_curr)
        # Generate a new goal.
        lego_position = self.get_lego_position()

        if self._goal_generator is not None:
            self.goal = self._goal_generator.generate_goal(lego_position)
            qpos[-goal_dims:] = self.goal[:goal_dims, None]
        else:
            print("No goal generator!")

        if self.allow_random_vel_restarts or self.first_time:
            # Generate a new random robot velocity.
            #qvel = self.init_qvel + np.random.normal(size=self.init_qvel.shape) * 0.1
            qvel = self.init_qvel + np.random.normal(size=self.init_qvel.shape) * self.qvel_init_std
            #qvel = self.init_qvel + np.random.normal(size=self.init_qvel.shape) * 10
        else:
            qvel = np.array(self.model.data.qvel)

        if self._lego_generator is not None:
            self.lego = self._lego_generator.generate_goal(lego_position)
            qpos[-goal_dims - lego_dims - 1:-goal_dims] = self.lego[:, None]
        else:
        # print("No lego generator!")
            qpos[-goal_dims - lego_dims - 1:-goal_dims] = np.array((0.6, 0.5, 0.5249, 1, 0, 0, 0))[:, None]

        # Set the velocity of the goal (the goal itself -
        # this is NOT the arm velocity at the goal position!) to 0.
        qvel[-goal_dims-lego_dims:] = 0

        #The position of a free body has 7 components (3 space and 4 for quaternions)

        self.model.data.qpos = qpos
        self.model.data.qvel = qvel
        self.model.data.qacc = self.init_qacc
        self.model.data.ctrl = self.init_ctrl

        if self._action_limiter is not None:
            self.action_limit = self._action_limiter.get_action_limit()

        self.first_time = False

        #Apply a force in the Lego block
        xfrc = np.zeros(self.model.data.xfrc_applied.shape)
        xfrc[-2, 2] = -0.981
        self.model.data.xfrc_applied = xfrc
        #Viewer
        if self.use_vision:
            if self.viewer is None:
                self.viewer = MjViewer()
                self.viewer.start()
                self.viewer.set_model(self.model)
                self.viewer_setup()
        if self.use_depth:
            if self.viewer_bot is None:
                self.viewer_bot = MjViewer(is_bot=True)
                self.viewer_bot.start()
                self.viewer_bot.set_model(self.model)
                self.viewer_setup(is_bot=True)
            # data = self.viewer_bot.get_depth_map()
            # imsave('data/local/imgs/lego' + '.png', data)

        # Choose an action limit from the range of action limits.
        #self.action_limit = np.random.uniform(self.min_action_limit, self.max_action_limit)

    def update_env(self, env_vars):
        self._goal_generator = env_vars.goal_generator
        self._action_limiter = env_vars.action_limiter

    def update_failure_rate(self, paths):
        distances_to_goal = [path["env_infos"]["distance_to_goal"] for path in paths]
        paths_within_thresh = np.mean([(d < self.distance_thresh).any() for d in distances_to_goal])
        failure_rate = 1 - paths_within_thresh
        if self.use_running_average_failure_rate:
            self.mean_failure_rate = self.mean_failure_rate * self.failure_rate_gamma + failure_rate * (1 - self.failure_rate_gamma)
        else:
            # We don't want to be dependent on the initial failure rate, so just use a large batch size.
            self.mean_failure_rate = failure_rate

    def get_mean_failure_rate(self):
        return self.mean_failure_rate

    @overrides
    def log_diagnostics(self, paths):
        actions = [path["actions"] for path in paths]
        logger.record_tabular('MeanAbsActions', np.mean(np.abs(actions)))
        logger.record_tabular('MaxAbsActions', np.max(np.abs(actions)))
        logger.record_tabular('StdAbsActions', np.std(np.abs(actions)))
        logger.record_tabular('StdActions', np.std(actions))

        #distances_to_goal_x = [path["env_infos"]["distance_to_goal_x"] for path in paths]
        #distances_to_goal_y = [path["env_infos"]["distance_to_goal_y"] for path in paths]
        #distances_to_goal_z = [path["env_infos"]["distance_to_goal_z"] for path in paths]

        #logger.record_tabular('FinalDistanceToGoalX', np.mean([d[-1] for d in distances_to_goal_x]))
        #logger.record_tabular('FinalDistanceToGoalY', np.mean([d[-1] for d in distances_to_goal_y]))
        #logger.record_tabular('FinalDistanceToGoalZ', np.mean([d[-1] for d in distances_to_goal_z]))


        # logger.record_tabular('MaxFinalDistanceToGoalX', np.max([d[-1] for d in distances_to_goal_x]))
        # logger.record_tabular('MaxFinalDistanceToGoalY', np.max([d[-1] for d in distances_to_goal_y]))
        # logger.record_tabular('MaxFinalDistanceToGoalZ', np.max([d[-1] for d in distances_to_goal_z]))
        distances_tip_to_lego = [path["env_infos"]["distance_tip_to_lego"] for path in paths]
        logger.record_tabular('MinFinalDistanceTipLego', np.min([d[-1] for d in distances_tip_to_lego]))
        logger.record_tabular('MinDistanceTipLego', np.mean([np.min(d) for d in distances_tip_to_lego]))

        distances_to_goal = [path["env_infos"]["distance_to_goal"] for path in paths]
        logger.record_tabular('MinDistanceToGoal', np.mean([np.min(d) for d in distances_to_goal]))
        logger.record_tabular('MinFinalDistanceToGoal', np.min([d[-1] for d in distances_to_goal]))
        logger.record_tabular('FinalDistanceToGoal', np.mean([d[-1] for d in distances_to_goal]))
        distances_to_goal = [path["env_infos"]["reward_dist"] for path in paths]
        logger.record_tabular('RewardDistanceLegoGoal', np.mean([np.sum(r) for r in distances_to_goal]))
        distances_to_goal = [path["env_infos"]["reward_tip"] for path in paths]
        logger.record_tabular('RewardDistanceLegoTip', np.mean([np.sum(r) for r in distances_to_goal]))
        distances_to_goal = [path["env_infos"]["reward_angle"] for path in paths]
        logger.record_tabular('RewardAngle', np.mean([np.sum(r) for r in distances_to_goal]))
        # The task is considered complete when we get within distance_thresh of the goal.
        #reached_goal_indices = np.where(distances_to_goal < distance_thresh)
        #if (distances_to_goal < distance_thresh).any():
        if any([(d < self.distance_thresh).any() for d in distances_to_goal]):
            #distance_to_thresh = np.argmin(distances_to_goal < distance_thresh)
            #np.mean([np.argmin(d < distance_thresh) for d in distances_to_goal if np.argmin(d < distance_thresh) > 0])
            steps_to_thresh = np.mean([np.argmax(np.array(d) < self.distance_thresh) for d in distances_to_goal if (d < self.distance_thresh).any()])
        else:
            steps_to_thresh = len(distances_to_goal[0]) + 1
        time_to_thresh = steps_to_thresh * self.frame_skip * self.model.opt.timestep
        logger.record_tabular('TimeToGoal', time_to_thresh)
        paths_within_thresh = np.mean([(d < self.distance_thresh).any() for d in distances_to_goal])
        logger.record_tabular('PathsWithinThresh', paths_within_thresh)

        # pos_dim = len(self.model.data.qpos.flat)
        # vel_dim = len(self.model.data.qvel.flat[:-3])
        # Timesteps,
        # observations = [path["observations"] for path in paths]
        #velocities = [path["observations"][:][pos_dim+1 : pos_dim+vel_dim] for path in paths]
        # velocities_nested = observations[:][pos_dim+1 : pos_dim+vel_dim]
        # velocities = list(itertools.chain.from_iterable(velocities_nested))
        # logger.record_tabular("MeanVelocities", np.mean(np.abs(velocities)))
        # logger.record_tabular("MaxVelocities", np.max(np.abs(velocities)))
        # print "Mean vel: " + str(np.mean(np.mean(np.abs(velocities), 1),1))
        # print "Max vel: " + str(np.max(np.max(np.abs(velocities), 1),1))

        # goal_generator_diagnostics = self._goal_generator.get_diagnostics()
        # for key, val in goal_generator_diagnostics.items():
        #   logger.record_tabular(key, val)

        # action_limiter_diagnostics = self._action_limiter.get_diagnostics()
        # for key, val in action_limiter_diagnostics.items():
        #    logger.record_tabular(key, val)

        # self.update_failure_rate(paths)

        # action_limit = self._action_limiter.get_action_limit()
        # failure_rate = self.get_mean_failure_rate()
        # expected_damage = action_limit * failure_rate
        # logger.record_tabular('Expected Damage', expected_damage)


