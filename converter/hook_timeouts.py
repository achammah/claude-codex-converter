"""Source command deadlines and the sequential adapter's enclosing budget."""
import math


WRAPPER_OVERHEAD_SECONDS = 30
NATIVE_MAX_SECONDS = (1 << 64) - 1


def source_timeout(handler, event):
    """Retain explicit seconds; use Claude's event-specific command default."""
    value = handler.get('timeout', {'UserPromptSubmit': 30, 'SessionEnd': 1.5}.get(event, 600))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)) or value <= 0:
        raise ValueError('Hook timeout must be a positive finite number of seconds')
    if math.ceil(value) > NATIVE_MAX_SECONDS - WRAPPER_OVERHEAD_SECONDS:
        raise ValueError('Hook timeout exceeds the Codex native seconds range including adapter overhead')
    return value


def wrapper_timeout(routes, event):
    # Failure hooks are dispatched through the native PostToolUse event. Sum all
    # candidates conservatively: scopes/matchers may overlap at invocation time.
    events = [event, 'PostToolUseFailure'] if event == 'PostToolUse' else [event]
    budget = sum(math.ceil(source_timeout(route['handler'], source_event))
                 for source_event in events for route in routes.get(source_event, [])) + WRAPPER_OVERHEAD_SECONDS
    if budget > NATIVE_MAX_SECONDS:
        raise ValueError('Combined hook timeout exceeds the Codex native seconds range; split or adapt the source handlers')
    return budget
