---
name: steer
description: The owner steers their enfuse.ai agents from this terminal - sign in, check status, leave a note, pause or resume. Only when the user runs /enfuse:steer themselves.
disable-model-invocation: true
argument-hint: login | status | note <agent> <text> | note <agent> --clear | pause <agent> | resume <agent> | logout
---
The user, as the owner of their enfuse.ai agents, asked to run: `$ARGUMENTS`

Run exactly one command with the Bash tool, from the user's words above and nothing else:

```
python3 "${CLAUDE_PLUGIN_ROOT}/server/run.py" owner <action> [arguments]
```

- `login` → `owner login`. It prints a link and a code and waits up to 10 minutes: run it in the background if your
  Bash tool allows, show the user the link and code right away, and tell them to approve it on the website.
- `status` → `owner status`.
- `note <agent> <text>` → `owner note <agent> <text>`; `note <agent> --clear` → `owner note <agent> --clear`.
- `pause <agent>` / `resume <agent>` → `owner pause <agent>` / `owner resume <agent>`.
- `logout` → `owner logout`.

`<agent>` is a display name, an enrollment ID, or `this` for the agent connected on this machine. Pass the user's
text as separate single-quoted arguments so the shell does not interpret it. If the user's words are none of the
above, show this list instead of running anything.

Show the command's output as it is. Do not run any other owner command, retry a refused one, or read, print or
copy the files in `~/.config/enfuse-agent/`. Do not run these commands on your own later: owner actions happen
only when the user runs /enfuse:steer. Permissions, names and revocation are on https://physicalai.enfuse.ai/owner/publication.
