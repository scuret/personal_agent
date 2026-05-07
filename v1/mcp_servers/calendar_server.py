"""Google Calendar MCP server — READ-ONLY in v1.

Exposes:
  - calendar_list_events(time_min?, time_max?, calendar_id?, max_results?)
  - calendar_search_events(query, time_min?, time_max?)
  - calendar_check_availability(time_min, time_max, calendar_ids?)
  - calendar_get_event(event_id, calendar_id?)

INTENTIONALLY MISSING in v1: create / update / delete event tools.
Calendar writes are deferred to v2 once read-side patterns are stable.

Built in step 4 of the v1 plan.
"""


def main() -> None:
    raise NotImplementedError("Calendar MCP server not yet implemented — see step 4 of the v1 plan.")


if __name__ == "__main__":
    main()
