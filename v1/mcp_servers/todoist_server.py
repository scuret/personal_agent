"""Todoist MCP server — task and project management.

Exposes:
  - todoist_list_tasks(filter, project_id?, limit) — uses Todoist filter syntax
                                                     (today, overdue, p1, etc.)
  - todoist_create_task(content, due_string?, priority?, project_id?, labels?)
  - todoist_update_task(task_id, ...)
  - todoist_complete_task(task_id)
  - todoist_list_projects()
  - todoist_list_labels()

Built in step 4 of the v1 plan.
"""


def main() -> None:
    raise NotImplementedError("Todoist MCP server not yet implemented — see step 4 of the v1 plan.")


if __name__ == "__main__":
    main()
