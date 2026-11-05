# Live model verification: chat, briefing and flags (2026-11-05)

The steps that needed the GPU: the step 2 live run (plan Task 9), a real briefing (step 4) and the
briefing's Flags section (step 6). Model: `qwen3.5:27b` on the workstation's Ollama.

## How the cluster reached the model

The workstation firewall still blocks inbound 11434, and that rule is the owner's decision. For
this test only, a reverse SSH tunnel from the workstation to the primary node carried Ollama to a
listener bound to the node's pod bridge address, so only pods on that node could reach it. The
assistant Deployment and the one-off jobs got a temporary `ASSISTANT_OLLAMA_URL` override. The
tunnel, the listener and the override were removed afterwards and the model was unloaded.

## Chat (step 2, Task 9)

- `POST https://assistant.lab/v1/chat/completions` with a helper-minted token, asking "What did I
  listen to this week?": the model called `homestudio_get_listening_history` through the gateway
  and answered from that data. The gateway audit log shows the call under the `personal-assistant`
  client, with the argument hash and result size.
- With the tunnel removed, the same request returns the offline sentence within seconds.
- Model pick: `qwen3.5:27b` produced the right tool call and a grounded answer on the first
  attempt, so it stays the default.

## Briefing (step 4) and Flags (step 6)

- First run (`pdl-assistant-briefing --now` as a one-off job from the CronJob): the briefing was
  sent with Email, Texts, Bank and Flags sections, but News came back as
  `unavailable (ReadTimeout)`.
- Cause: the model's thinking mode. A summary prompt of about 1,500 tokens produced roughly 3,600
  tokens of hidden reasoning before a 60-token answer: 94 seconds against a 120-second timeout.
  With thinking off the same call takes about 2 seconds.
- Fix: `model_think` (`ASSISTANT_MODEL_THINK`), which passes Ollama's `think` flag; unset keeps
  the model's default. The cluster's `assistant-config` now sets it to `false`.
- Second run, on the fixed image with thinking off: every section rendered and the whole briefing
  took about 6 seconds of model time. Before it, a synthetic over-threshold transaction was
  inserted and the flagger run once (one notification). The briefing's Flags section then listed
  that flag's summary line, which exercises "flags raised since the previous briefing" against a
  real earlier briefing.
- Chat with thinking off: the same listening question still produced the tool call and a grounded
  answer, in 16 seconds instead of 84.

The synthetic bank rows, the flag row and the test jobs were removed afterwards.

## Still open

- The scheduled 07:30 briefing and day-to-day chat need the cluster to reach Ollama permanently,
  which means the inbound firewall rule on the workstation (TCP 11434, limited to the primary
  node's address) or another standing path. Until then the briefing records `skipped` and chat
  answers with the offline sentence.
- Notifications arriving on the phone (ntfy app), the SMS forwarder on the phone, IMAP details and
  real bank statements, as listed in the step 3 to 6 verification notes.
- Not done here: the plain-process run of the assistant on the workstation and the web UI chat
  after the gateway login with the model online (the login flow and the chat API were each
  verified separately).
