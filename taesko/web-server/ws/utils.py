import copy
import logging
import collections

import ws.logs


class StateWouldBlockException(Exception):
    pass


class StateMachineBroke(Exception):
    pass


class StateMachine:
    def __init__(self, machine_table, initial_transition):
        assert isinstance(machine_table, collections.Sequence)
        assert isinstance(initial_transition, str)

        self.states = copy.deepcopy(machine_table)
        self.transitions = {}
        self.broken = False

        for state in machine_table:
            for trans in state.transitions:
                assert trans.name not in self.transitions
                self.transitions[trans.name] = trans

        self.transition = self.transitions[initial_transition]
        assert self.verify()

    def verify(self):
        for name, state in self.states.items():
            assert name == state.name
            assert isinstance(state, State)
            assert isinstance(state.name, str)
            assert isinstance(state.final, bool)
            assert not state.final and state.transitions
            for trans in state.transitions:
                assert isinstance(trans.next, str)
                assert trans.next in self.states
                assert isinstance(trans.callback, collections.Callable)
        assert isinstance(self.transition, Transition)

    def current_state(self):
        for state in self.states.values():
            if self.transition.name in state.transitions:
                return state
        assert False


    def finished(self):
        return self.current_state().final

    def run(self, *args, **kwargs):
        if self.broken:
            raise StateMachineBroke(msg='Prior call to run() failed with an '
                                        'unhandled exception. Cannot continue '
                                        'operation.')
        while not self.finished():
            ws.logs.error_log.debug('Executing transition %s to %s.',
                                    self.transition.name,
                                    self.transition.next)
            try:
                new_trans_name = self.transition.callback(*args, **kwargs)
            except StateWouldBlockException:
                ws.logs.error_log.debug('Transition %s to %s would '
                                        'have blocked. Returning from run() '
                                        'without advancing state.',
                                        self.transition.name,
                                        self.transition.next)
                break
            except:
                self.broken = True
                raise
            assert new_trans_name in self.current_state().transitions
            self.transition = self.transitions[new_trans_name]
        ws.logs.error_log.debug('State %s is final. Returning from run() '
                                'method..', self.transition.current)


State = collections.namedtuple('State', ['name', 'final', 'callback',
                                         'transitions'])
Transition = collections.namedtuple('Transition', ['name', 'callback', 'next'])

def depreciated(log=ws.logs.error_log):
    assert isinstance(log, logging.Logger)

    def decorator(func):
        def wrapped(*args, **kwargs):
            log.warning('Calling depreciated function %s.'
                        .format(func.__name__))
            return wrapped(*args, **kwargs)

        return wrapped
    return decorator


