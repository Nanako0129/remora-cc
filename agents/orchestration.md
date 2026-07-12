# remora session orchestration

Main-session policy. If you are running as a subagent role (`Explore`, `scout`, `mech-executor`, `executor`, `verifier`, or `security-executor`), ignore this section and complete the task yourself without further delegation.

Use the supplied role agents for execution while keeping planning, architecture, ambiguity resolution, integration, and final review in the main session. Choose `Explore` or `scout` for read-only reconnaissance, `mech-executor` for fully specified mechanical work, `executor` for implementation requiring local judgment, `verifier` for fresh-context verification, and `security-executor` for security-sensitive work.

Schedule delegation by data dependency, not by whether the result will eventually be needed:

- If the main session can make useful progress before an agent returns, invoke that agent with `run_in_background: true` and continue working.
- When dispatching two or more independent agents, launch them as one parallel batch with `run_in_background: true` on every call. Give each writing agent an isolated worktree and integrate its changes after completion; read-only agents may share the checkout.
- Use foreground execution only when the very next main-session action cannot proceed without that agent's result and there is no other useful independent work to do. Do not use foreground merely because the result will be needed later.
- A background launch is not a completed result. Track it, collect its output before any dependent action or final answer, and resume the agent when follow-up is required. Do not poll while other useful work remains.
