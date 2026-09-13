"""The closed approval vocabulary `turn.resolve` may carry, and its mapping.

Two vocabularies meet at the worker boundary, and until now nothing joined them.

Cortex's is `approve_once | deny`. It is what `translate_hermes_signal` offers
the operator (`runtime/events.py:118`), what the adapter already refuses to
leave (`runtime/hermes.py:2062`), and what the shared projection digests
(`digests.DECISION_OPTIONS`).

The fork's is `once | session | always | deny`
(`tools/approval.py:prompt_dangerous_approval`), and two of those four grant
*standing* permission: the answer is remembered per session key, so one operator
decision silently pre-approves every later dangerous command in that session.
The compatibility matrix has promised for four slices that the bridge "never
returns session|always" — but the in-process backend kept that promise with a
line of its own (`hermes.py:1055`) and the managed one did not keep it at all:
`ForkRunner.approval` returned whatever string arrived over the channel, so a
`turn.resolve` carrying `"always"` would have been handed straight to the fork.

So the wire vocabulary is closed at Cortex's two, and the translation to the
fork's happens once, at the point the worker answers the callback. Enforced
three times over, deliberately: product-side before the frame is written, in the
protocol grammar on the way in, and again at the mapping point. A choice outside
the set is refused rather than translated into something safer and forwarded —
the operator asked for something this system does not offer, and saying so is
the answer.

Shipped inside the payload because the mapping belongs where the fork is
imported, and read product-side through `worker_payload` so both ends share one
source file, the way `digests.py` already is.
"""

from __future__ import annotations

#: Approve exactly this one action, and nothing after it.
CHOICE_APPROVE_ONCE = "approve_once"
#: Refuse this action. Also the answer to a cancelled turn and to a decision that
#: never arrives, so it is the only safe default.
CHOICE_DENY = "deny"

#: The whole wire vocabulary. Closed, ordered, and the single definition the
#: product, the protocol grammar and the mapping all validate against.
APPROVAL_CHOICES = (CHOICE_APPROVE_ONCE, CHOICE_DENY)

#: The fork's strings this product will not produce, named rather than merely
#: absent so the reason is greppable from either side. `session` and `always`
#: grant permission beyond the action the operator was shown.
STANDING_CHOICES = ("session", "always")

#: The single mapping onto `tools/approval.py`'s vocabulary. The in-process
#: backend spells the same map inline at `runtime/hermes.py:1055`; this is that
#: line, moved across the process boundary.
_FORK_CHOICES = {CHOICE_APPROVE_ONCE: "once", CHOICE_DENY: "deny"}

#: What the fork's approval callback may return, once mapped. Never
#: `STANDING_CHOICES`.
FORK_CHOICES = tuple(sorted(set(_FORK_CHOICES.values())))


class ApprovalChoiceError(ValueError):
    """A decision named a choice outside the closed set."""


def is_approval_choice(choice: object) -> bool:
    return isinstance(choice, str) and choice in APPROVAL_CHOICES


def validate_approval_choice(choice: object) -> str:
    if not is_approval_choice(choice):
        raise ApprovalChoiceError("approval choice is not in the closed set")
    return str(choice)


def fork_choice(choice: object) -> str:
    """Map a validated choice onto the fork's vocabulary, fail-closed.

    Anything unrecognised becomes the fork's `deny`. By the time control reaches
    here the choice has been checked twice; a third outcome would mean one of
    those checks was bypassed, and the safe reading of a bypassed check is
    refusal — never a standing grant.
    """

    return _FORK_CHOICES.get(str(choice), _FORK_CHOICES[CHOICE_DENY])
