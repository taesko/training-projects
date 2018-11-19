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
    If an exception is raised from a callback the machine supports transition to
    a default error state.
    See help of __init__ for parameter description and setup.
    """

    def __init__(self, states, callbacks, initial_state, state_on_exc,
                 allowed_exc_handling):
        """ Setup the state machine - run() needs to be explicitly called.

        :param states: A mapping of one state to a sequence of states it can
            transition to. If a state is final the sequence must be empty.
            The first state in the sequence is assumed to be the default
            state to transition to when self.transition_to() is not explicitly
            called.
        :param callbacks: A mapping between a state and it's callback.
            States that are final MUST NOT be in here.
        :param initial_state: Name of the first state.
        :param state_on_exc: Name of an exception handling state.
        If an exception is raised from inside a callback the machine will
        set the self.exception variable and transition to this state.
        :param allowed_exc_handling: Collection of states from which
        automatic transitioning to state_on_exc is allowed.
        """
        assert isinstance(states, collections.Mapping)
        assert isinstance(callbacks, collections.Mapping)
        assert isinstance(initial_state, str)
        assert isinstance(state_on_exc, str)
        assert initial_state in states
        assert state_on_exc in states

        # make shallow copies to guarantee the fields won't be changed
        # externally.
        states = {st: tuple(trans) for st, trans in states.items()}
        callbacks = dict(callbacks)
        allowed_exc_handling = frozenset(allowed_exc_handling)

        for state, allowed_transitions in states.items():
            assert isinstance(state, str)
            for s in allowed_transitions:
                assert s in states

            if not states[state]:
                msg = '{} is a final state but has a callback'
                assert state not in callbacks, msg.format(state)
            else:
                assert state in callbacks

            # TODO algorithm for checking if a graph is walkable from each
            # node to every other node
            # if state not in (initial_state, state_on_exc):
            #     for other_state, other_allowed in states.items():
            #         if other_state != state and state in other_allowed:
            #             break
            #     else:
            #         assert False, 'No transition to {}'.format(state)
        for state, cb in callbacks.items():
            assert state in states
            assert isinstance(cb, collections.Callable)
        for state in allowed_exc_handling:
            assert state != state_on_exc
            assert state in states

        self.states = states
        self.callbacks = callbacks
        self.state_on_exc = state_on_exc
        self.allowed_exc_handling = allowed_exc_handling
        # below fields need to be reset on every transition.
        self.state = initial_state
        self.explicit_transition = None
        self.exception = None

    def finished(self):
        return not bool(self.states[self.state])

    def transition_to(self, state):
        assert state in self.states
        self.explicit_transition = state

    def throw(self, exception):
        if isinstance(exception, self.unhandleable_exceptions):
            raise exception
        elif self.state == self.state_on_exc:
            raise exception
        elif self.state not in self.allowed_exc_handling:
            raise exception

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
        self.exception = exception

    def run(self, *args, **kwargs):
        assert not self.finished()
        ws.logs.error_log.debug3('StateMachine: Running %s',
                                 self.state)
        try:
            result = self.callbacks[self.state](*args, **kwargs)
        except StateWouldBlockException:
            raise
        except BaseException as err:
            next_state = self.state_on_exc
            ws.logs.error_log.debug('StateMachine: %s > EXCEPTION > %s',
                                    self.state,
                                    next_state)
            self.state = next_state
            self.explicit_transition = None
            self.exception = err
            raise
        else:
            if self.explicit_transition:
                transition = self.explicit_transition
            else:
                transition = self.states[self.state][0]
            ws.logs.error_log.debug2('StateMachine: %s > %s',
                                     self.state,
                                     transition)
            self.state = transition
            self.explicit_transition = None
            self.exception = None
            return result


def depreciated(log=ws.logs.error_log):
    assert isinstance(log, logging.Logger)

    def decorator(func):
        def wrapped(*args, **kwargs):
            log.warning('Calling depreciated function %s.'
                        .format(func.__name__))
            return wrapped(*args, **kwargs)

        return wrapped

    return decorator
