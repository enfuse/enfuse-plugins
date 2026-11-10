---
name: status
description: Show this machine's enfuse.ai agent - whether it is claimed and active, its owner's note, its permissions and whether it is paused. Use when the user asks about their enfuse.ai agent's status or what their note says.
---
Call `enfuse_status` and tell the user, in a few lines:

- whether the agent is registered, waiting for a claim, or active (and its public name);
- the owner's note, if there is one, and when it was set;
- which permissions are on (hardware experiments, power modes, containers) and whether participation is paused.

If it is not registered, offer `/enfuse:connect`. The owner can leave a note, pause or resume from this terminal with `/enfuse:steer` (they run it themselves); permissions and revocation are at https://physicalai.enfuse.ai/owner/publication. Never show or ask for the registration key.
