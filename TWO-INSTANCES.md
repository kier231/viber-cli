# Local Viber instances

The controller is available at http://127.0.0.1:4001/viber. Select **Viber 1** or **Viber 2** under **Sending account**.

- `ViberWorker1` and `ViberWorker2` run in separate local VirtualBox VMs, each with 2 GB RAM and 2 CPUs. Their authenticated bridge ports are 4012 and 4013 on host loopback only.
- Link each VM to a different Viber number using its QR code. The agent opens registration before attempting database key capture. After linking, it confirms the old Viber process has stopped, captures the reader key, and verifies the native account identity before activation.
- Each account has its own inbox, contact ownership, campaigns, schedules, reply settings and durable desktop queue. Two Codex generations can run across the controller; Viber actions remain serialized within each account.
- Add contacts in **Contacts → Add a Viber contact**. The selected VM verifies the number and Viber name before saving the dashboard record and account ownership together. No USB phone or Android address-book access is needed, and adding a contact never sends a message.
- Recipient navigation uses the exact saved phone number, with each dial-pad digit read back. A different Viber nickname or profile name does not block a send. The current chat must remain stable while the message is prepared and dispatched; its observed name is recorded with the send.
- Automatic replies are enabled by default for each verified new account and remain enabled across updates and restarts. Pause them only on the owner's explicit request or dashboard action. Completed Codex replies send automatically after fresh conversation, ownership and recipient checks. Private assumption questions never delay a valid reply. Manual messages and outreach batches require review before sending/activation. Uncertain sends are never retried automatically.
- The old `current` account is disabled, and `ViberWorker` remains powered off. Its eight existing lead records remain assigned to that account and are ignored by the new workers.
- `SajtologViberController` starts the controller and the two new VM desktops at Windows sign-in. Each VM runs its own `SajtologViberWorker` task at guest sign-in.

Configuration is recorded in `C:\viber-cli\data\instances.json`. Private bridge tokens are stored in the restricted `C:\viber-cli\private` directory; do not share those files.

The PostgreSQL dump, baseline counts and previous controller files are retained under the backup directory referenced by the manifest. The original VM snapshot is the base for both linked clones: keep it and its disks while either clone is in use. Before rollback, stop both controller workers and reconcile any sends after the backup so they cannot be dispatched twice.
