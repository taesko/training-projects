import collections
import logging

import ws.logs


class StateWouldBlockException(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.msg = msg
        self.code = code


class StateMachine:
    """ State machine pattern that supports multiple transitions and error handling.

    User's can transition to multiple possible states by calling the
    self.transition_to() method from inside a state callback.
    If an exception is raised from a callback the machine can transition to
    a default error state.
    See help of __init__ for parameter description and setup.
    """


    def __init__(self, states, callbacks, initial_state, state_on_exc):
        """ Setup the state machine - run() needs to be explicitly called.

        :param states: A mapping of one state to a sequence of states it can
            transition to. If a state is final the sequence must be empty.
            The first state in the sequence is assumed to be the default
            state to transition to when self.transition_to() is not explicitly
            called.
        :param callbacks: A mapping between a state and it's callback.
            States that are not final should not be in here.
        :param initial_state: Name of the first state.
        :param state_on_exc: A mapping to configure exception handling states.
            It MUST contain a SINGLE key which is the name of the error state.
            The value must be a dict with two keys who's values are sequences:
                from: states from which the machine will transition to an error
                    if an exception is raised.
                to: states to which the machine can transition after error
                    handling.
        """
        assert isinstance(states, collections.Mapping)
        assert isinstance(callbacks, collections.Mapping)
        assert isinstance(initial_state, str)
        assert isinstance(state_on_exc, str)
        assert initial_state in states
        assert state_on_exc in states

        for state, allowed_transitions in states.items():
            assert isinstance(state, str)
            for s in allowed_transitions:
                assert s in states

            if not states[state]:
                assert state not in callbacks
            if state == initial_state:
                assert state not in callbacks
            assert state in callbacks

            if state not in (initial_state, state_on_exc):
                for other_state, other_allowed in states.items():
                    if other_state != state and other_state in other_allowed:
                        break
                else:
                    assert False, 'No transition to {}'.format(state)
        for state, cb in callbacks.items():
            assert state in states
            assert isinstance(cb, collections.Callable)

        self.states = states
        self.callbacks = callbacks
        self.state_on_exc = state_on_exc
        # below fields need to be reset on every transition.
        self.state = self.states[initial_state]
        self.explicit_transition = None
        self.exception = None

    def finished(self):
        return bool(self.states[self.state])

    def transition_to(self, state):
        assert state in self.states
        self.explicit_transition = state

    def run(self, *args, **kwargs):
        while not self.finished():
            assert isinstance(self.state, State)
            ws.logs.error_log.debug3('StateMachine: Running %s',
                                     self.state)
            try:
                self.callbacks[self.state](*args, **kwargs)
            except StateWouldBlockException as err:
                ws.logs.error_log.debug3('StateMachine: %s would block. '
                                         'CODE=%s MSG=%s',
                                         self.state, err.code, err.msg)
                break
            except BaseException as err:
                if self.state == self.state_on_exc:
                    raise
                # It's possible to enter an infinite loop here because
                # a state after error throws an error again.
                # TODO guarantee this doesn't happen without reducing
                # flexibility
                next_state = self.state_on_exc
                ws.logs.error_log.debug('StateMachine: %s > EXCEPTION > %s',
                                        self.state,
                                        next_state)
                self.state = next_state
                self.explicit_transition = None
                self.exception = err
            else:
                if self.explicit_transition:
                    transition = self.explicit_transition
                else:
                    transition = next(self.states[self.state])
                next_state = self.states[transition.next]
                ws.logs.error_log.debug2('StateMachine: %s ~ %s > %s',
                                         self.state,
                                         transition,
                                         next_state)
                self.state = next_state
                self.explicit_transition = None
                self.exception = None
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
