from rllab.envs.base import Env
from rllab.envs.base import Step
from rllab.spaces import Box
import numpy as np

class BimodEnv(Env):
    @property
    def observation_space(self):
        return Box(low= -np.inf, high=np.inf, shape=(1,))

    @property
    def action_space(self):
        return Box(low=-5.0, high=5.0, shape=(1,))

    def reset(self):
        self._state = np.zeros(shape=(1,))
        observation = np.copy(self._state)
        return observation

    def step(self, action):
        d = 1
        self._state = self._state + action
        x, = self._state
        done = True
        next_observation = np.copy(self._state)
        reward = 1./(2.*np.sqrt(2.*np.pi*0.1))*(np.exp(-0.5/0.1*(x-d)**2)+np.exp(-0.5/0.1*(x+d)**2)) - 0.5
        return Step(observation=next_observation, reward=reward, done=done)

    def render(self):
        print 'current state:', self._state