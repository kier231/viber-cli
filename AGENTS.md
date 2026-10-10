# Owner preferences

- Automatic Codex replies are enabled by default for verified replacement Viber accounts and send without individual approval.
- Do not disable replies unless the owner explicitly asks, including an explicit dashboard pause. Preserve saved enablement across updates and restarts; use graceful service shutdown for maintenance without changing the persisted flag.
- Keep account ownership, fresh conversation and recipient verification, opt-out handling, and uncertain-send protections. An unsafe or uncertain job must not send or retry automatically.
- A cached Viber display name is informational. Opening the exact saved phone number with verified dial-pad readback may yield a different stable display name; that difference alone must not block opening, review or sending. Keep live-chat stability checks during dispatch.
- The archived `current` account stays disabled and its old contacts remain ignored. This preference applies to the two replacement instances.
- Confirmed pre-Send temporary failures retry durably with fresh ownership and conversation checks. Reuse a valid generated reply when its context is unchanged. Never retry a durable attempted send or an unconfirmed draft cleanup.
