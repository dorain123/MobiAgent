Evaluate this path-level multimodal trajectory.

Metadata:
- app_name: {{APP_NAME}}
- task_type: {{TASK_TYPE}}
- sample_id: {{SAMPLE_ID}}
- task_description: {{TASK_DESCRIPTION}}
- action_count: {{ACTION_COUNT}}
- step_count: {{STEP_COUNT}}
- image_count: {{IMAGE_COUNT}}
- ends_with_done: {{ENDS_WITH_DONE}}
- configured_depth_limit: {{CONFIGURED_DEPTH_LIMIT}}
- configured_breadth: {{CONFIGURED_BREADTH}}
- stats: {{STATS_JSON}}

Read the following content in order.
For each step, read the step text first, inspect the original screenshot, and then inspect the click-point screenshot if one is provided.
Use the click-point screenshot to judge whether the operation lands on the intended UI element.
Base the final judgment on the full path, the task description, the operation sequence, and the visual evidence, not on a single step or a final done status alone.
