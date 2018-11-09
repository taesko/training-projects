import collections
import logging

import ws.logs


class StateWouldBlockException(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.msg = msg
        self.code = code


class StateMachine:
    """ State machine pattern that supports transition to multiple states.

    """

    def __init__(self, machine_table, initial_state):
        assert isinstance(machine_table, collections.Sequence)
        assert isinstance(initial_state, str)

        self.states = {}

        for state in machine_table:
            self.states[state.name] = state
            assert isinstance(state, State)
            if state.on_exception:
                assert state.on_exception in [state.name
                                              for state in machine_table]

        self.state = self.states[initial_state]
        self.exception = None

    def finished(self):
        return self.state.final

    def run(self, *args, **kwargs):
        while not self.finished():
            assert isinstance(self.state, State)
            ws.logs.error_log.debug3('StateMachine: Running %s',
                                     self.state)
            try:
                new_trans_name = self.state.callback(*args, **kwargs)
            except StateWouldBlockException as err:
                ws.logs.error_log.debug3('StateMachine: %s would block. '
                                         'CODE=%s MSG=%s',
                                         self.state, err.code, err.msg)
                break
            except BaseException as err:
                # It's possible to enter an infinite loop here because
                # a state after error throws an error again.
                # TODO guarantee this doesn't happen without reducing
                # flexibility
                if self.state.on_exception:
                    next_state = self.states[self.state.on_exception]
                    ws.logs.error_log.debug('StateMachine: %s > EXCEPTION > %s',
                                            self.state,
                                            next_state)
                    self.state = next_state
                    self.exception = err
                else:
                    raise
            else:
                self.exception = None
                transition = next(t for t in self.state.transitions
                                  if t.name == new_trans_name)
                next_state = self.states[transition.next]
                ws.logs.error_log.debug2('StateMachine: %s ~ %s > %s',
                                         self.state,
                                         transition,
                                         next_state)
                self.state = next_state
        else:
            ws.logs.error_log.debug('StateMachine: %s is FINAL.', self.state)


class State:
    def __init__(self, name, final, callback, transitions=None,
                 on_exception=None):
        assert isinstance(name, str)
        assert isinstance(final, bool)
        if final:
            assert not callback
            assert not transitions
        else:
            assert isinstance(callback, collections.Callable)
            assert transitions
        self.name = name
        self.final = final
        self.callback = callback
        self.on_exception = on_exception
        if transitions:
            self.transitions = []
            for trans_name, next_state in transitions.items():
                t = Transition(name=trans_name, next=next_state)
                self.transitions.append(t)
        else:
            self.transitions = []

    def __str__(self):
        return 'State<{self.name}>'.format(self=self)


class Transition:
    def __init__(self, name, next):
        assert isinstance(name, str)
        assert isinstance(next, str)
        self.name = name
        self.next = next

    def __str__(self):
        return 'Transition<{self.name}>'.format(self=self)


def depreciated(log=ws.logs.error_log):
    assert isinstance(log, logging.Logger)

    def decorator(func):
        def wrapped(*args, **kwargs):
            log.warning('Calling depreciated function %s.'
                        .format(func.__name__))
            return wrapped(*args, **kwargs)

        return wrapped

    return decorator
