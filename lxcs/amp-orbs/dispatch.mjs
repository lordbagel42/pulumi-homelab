#!/usr/bin/env node
// Official E2B dispatcher: https://ampcode.com/docs/orbs/self-hosted
import { Sandbox } from 'e2b'

const {
	ORB_OPERATION, ORB_ID, ORB_TOKEN, AMP_URL, E2B_TEMPLATE,
} = process.env
const metadata = { orbId: ORB_ID }
const [existing] = await Sandbox.list({
	query: { metadata, state: ['running', 'paused'] },
}).nextItems()

if (ORB_OPERATION === 'spawn') {
	const timeoutMs = 60 * 60 * 1000
	const sandbox = existing
		? await Sandbox.connect(existing.sandboxId, { timeoutMs })
		: await Sandbox.create(E2B_TEMPLATE, {
			metadata, timeoutMs, lifecycle: { onTimeout: 'pause' },
		})
	const envs = { ORB_TOKEN }
	if (AMP_URL) envs.AMP_URL = AMP_URL
	const bootstrap = await sandbox.commands.run('amp orb bootstrap', {
		user: 'user', envs, background: true, timeoutMs: 0,
	})
	await bootstrap.disconnect()
} else if (ORB_OPERATION === 'pause' && existing) {
	await Sandbox.pause(existing.sandboxId)
} else if (ORB_OPERATION === 'delete' && existing) {
	await Sandbox.kill(existing.sandboxId)
}
