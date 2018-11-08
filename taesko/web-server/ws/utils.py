import copy
import logging
import collections

import ws.logs


def depreciated(log=ws.logs.error_log):
    assert isinstance(log, logging.Logger)

    def decorator(func):
        def wrapped(*args, **kwargs):
            log.warning('Calling depreciated function %s.'
                        .format(func.__name__))
            return wrapped(*args, **kwargs)

        return wrapped
    return decorator


class StateMachine:
    State = collections.namedtuple('State', ['name', 'transitions', 'final'])
    Transition = collections.namedtuple('Transition', ['name',
                                                       'current_state',
                                                       'callback',
                                                       'next_state'])

    def __init__(self, machine_table, initial, initial_transition):
        assert isinstance(machine_table, collections.Mapping)
        assert isinstance(initial, str)
        assert initial in machine_table

        self.states = copy.deepcopy(machine_table)
        self.transitions = {}

        for state in machine_table:
            for trans in state.transitions:
                self.transitions[trans.name] = trans

        self.machine_table = machine_table
        self.state = self.machine_table[initial]
        self.transition = initial_transition
        assert self.verify()

    def verify(self):
        for name, state in self.machine_table.items():
            assert name == state.name
            assert isinstance(state, self.State)
            assert isinstance(state.name, str)
            assert not state.final and state.transitions
            for trans in state.transitions:
                assert isinstance(trans.current_state, str)
                assert isinstance(trans.next_state, str)
                assert trans.current_state in self.machine_table
                assert trans.next_state in self.machine_table
                assert isinstance(trans.callback, collections.Callable)
                assert trans.current_state == state.name
        assert isinstance(self.transition, self.Transition)
        assert self.transition in self.states[self.transition.current_state]

    def run(self, *args, **kwargs):
        assert self.transition.current_state == self.state
        while not self.state.final:
            new_trans = self.transition.callback(*args, **kwargs)
            new_state = self.machine_table[new_trans.current_state.name]
            assert isinstance(new_trans, self.Transition)
            assert self.transition.next_state == new_trans.current_state
            assert new_trans in self.machine_table[new_trans.current_state]
            self.state = self.transition.next_state
            self.transition = new_trans


class StateWouldBlockException(Exception):
    pass
