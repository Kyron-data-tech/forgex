# ForgeX Workflow Template

Use this template to add a new website RPA workflow to ForgeX.

## How to create a new workflow:
1. Copy this folder to `workflows/<your_workflow_name>/` (do NOT start with an underscore).
2. Update `name`, `title`, `description`, and `content_types` in `workflow.py`.
3. Define the input fields in `get_input_schema()` for the "New Job" page.
4. Define the review fields in `get_review_schema()` for the human-approval gate.
5. Implement `prepare(inputs)`: research, API calls, data scraping, candidate image ranking.
6. Store resilient UI locators (roles, placeholders, aria labels) in `selectors.yaml`.
7. Implement `dry_run(data, page)`: fill forms and take screenshot, stopping before final submit.
8. Implement `publish(data, page)`: authorized final submission and URL verification.
9. Restart the ForgeX app or trigger workflow reload; it will be auto-discovered!
