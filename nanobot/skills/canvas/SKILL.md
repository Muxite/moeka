---
name: canvas
description: "Interact with Canvas LMS — list courses, assignments, grades, announcements, files, inbox, and more via the Canvas REST API."
metadata: {"nanobot":{"emoji":"🎓","requires":{"env":["CANVAS_API_URL","CANVAS_API_TOKEN"],"bins":["curl","jq"]}}}
---

# Canvas LMS Skill

Talk to Canvas via its REST API. Two environment variables are required — both
must be listed under `tools.exec.allowedEnvKeys` in `config.json` so shell
commands can read them:

| Variable            | Example                             | Purpose                    |
|---------------------|-------------------------------------|----------------------------|
| `CANVAS_API_URL`    | `https://canvas.instructure.com`    | Institution's Canvas host  |
| `CANVAS_API_TOKEN`  | (from Canvas → Account → Settings)  | Personal access token      |

All requests below use:

```text
Authorization: Bearer $CANVAS_API_TOKEN
```

Quick sanity check before anything else:

```bash
curl -sf -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/users/self" | jq '{id, name, primary_email}'
```

If that prints your user record, the skill is wired correctly.

## Courses

List active courses:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses?enrollment_state=active&per_page=50" \
  | jq '.[] | {id, name, course_code}'
```

## Assignments

List assignments for a course (replace `COURSE_ID`):

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/assignments?per_page=50&order_by=due_at"
```

Upcoming to-dos across all courses:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/users/self/todo?per_page=50"
```

Missing / late submissions:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/users/self/missing_submissions?per_page=50"
```

## Grades & submissions

Submissions + grades for a course:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/students/submissions?student_ids[]=self&include[]=assignment&per_page=50"
```

Current enrollment grade for a course:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/enrollments?user_id=self"
```

## Announcements

Recent announcements for a single course:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/discussion_topics?only_announcements=true&per_page=20&order_by=recent_activity"
```

Cross-course announcements:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/announcements?context_codes[]=course_COURSE_ID&per_page=20"
```

## Inbox / conversations

List inbox:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/conversations?scope=inbox&per_page=20"
```

Read a single conversation (messages + participants):

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/conversations/CONVO_ID"
```

Reply to a conversation (form-encoded — Canvas' default body format):

```bash
curl -s -X POST -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  --data-urlencode "body=Your reply here" \
  "$CANVAS_API_URL/api/v1/conversations/CONVO_ID/add_message"
```

Start a new conversation with a user:

```bash
curl -s -X POST -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  --data-urlencode "recipients[]=USER_ID" \
  --data-urlencode "subject=Subject" \
  --data-urlencode "body=Message body" \
  "$CANVAS_API_URL/api/v1/conversations"
```

## Files & pages

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/files?per_page=50"

curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/pages?per_page=50"
```

## Discussions

List topics:

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/discussion_topics?per_page=50"
```

Post an entry (JSON body, so the `Content-Type` header matters):

```bash
curl -s -X POST \
  -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Your post here"}' \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/discussion_topics/TOPIC_ID/entries"
```

## Modules & progress

```bash
curl -s -H "Authorization: Bearer $CANVAS_API_TOKEN" \
  "$CANVAS_API_URL/api/v1/courses/COURSE_ID/modules?include[]=items&per_page=50"
```

## Tips

- Pagination: Canvas returns a `Link:` header — follow `rel="next"` to collect further pages. `per_page` caps at 100.
- `COURSE_ID` is visible in any Canvas URL: `…/courses/<COURSE_ID>`.
- Prefer `--data-urlencode key=value` for Canvas' form endpoints (conversations, assignments); prefer `-H 'Content-Type: application/json' -d '{…}'` for modern JSON endpoints (discussions, quizzes).
- Use `jq` liberally to keep responses scannable: e.g. `| jq '.[] | {id, name, due_at}'`.
