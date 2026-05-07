"""Trigger scheduler — fires scheduled events to the agent host.

Two scheduled triggers in v1:
  - Morning brief: weekday mornings between 7-9 AM (configurable in .env).
    Pulls today's calendar, top tasks, and unread emails matching urgency
    criteria; the agent summarizes into a short iMessage.
  - Weekly review: Sunday evening (default 8 PM). Surfaces incomplete
    tasks and the upcoming week's calendar.

Triggers do NOT modify state; they only produce a notification message
that gets routed to the iMessage relay.

Built in step 5 of the v1 plan.
"""


def main() -> None:
    raise NotImplementedError("Trigger scheduler not yet implemented — see step 5 of the v1 plan.")


if __name__ == "__main__":
    main()
