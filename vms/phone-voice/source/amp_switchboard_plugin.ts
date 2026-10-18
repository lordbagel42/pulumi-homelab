import type { PluginAPI, ThreadID } from '@ampcode/plugin'
import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'

export const description = 'Connect Amp runner threads to the owner’s Switchboard phone for questions, steering, and acknowledged stop requests.'

type Reply = Record<string, any>
type PhoneSession = { id: number; extension: string }
type Command = { id: string; thread_id: string; action: string; text?: string; expires: number }
type Config = { url: string; token_file: string }
const nativeID = /^T-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i
const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

async function configuration(): Promise<Config | null> {
	let file: Partial<Config> = {}
	try {
		file = JSON.parse(await readFile(process.env.AMP_SWITCHBOARD_CONFIG || join(homedir(), '.config/amp/switchboard.json'), 'utf8'))
	} catch (error: any) {
		if (error.code !== 'ENOENT') throw new Error('Cannot read the Switchboard plugin configuration')
	}
	const url = process.env.PHONE_PLATFORM_URL || file.url
	const token_file = process.env.PHONE_PLATFORM_TOKEN_FILE || file.token_file
	if (!url && !token_file) return null
	if (!url || !token_file) throw new Error('Configure both Switchboard URL and token_file')
	const parsed = new URL(url)
	if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) {
		throw new Error('Switchboard URL must be an HTTP(S) base URL without credentials, query, or fragment')
	}
	return { url: url.replace(/\/+$/, ''), token_file }
}

export default async function (amp: PluginAPI) {
	const config = await configuration()
	if (!config) return
	const clientID = randomUUID()
	const threads = new Set<string>()
	const sessions = new Map<string, PhoneSession>()
	const acknowledgments = new Map<string, Reply>()
	const stopping = new AbortController()
	let stopped = false
	let lastWarning = 0

	async function rpc(method: string, params: Reply): Promise<Reply> {
		const token = (await readFile(config!.token_file, 'utf8')).trim()
		if (!token || /[\r\n]/.test(token)) throw new Error('Switchboard token file is invalid')
		const response = await fetch(config!.url + '/api/v1/phone/rpc', {
			method: 'POST', redirect: 'error',
			headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
			body: JSON.stringify({ method, params }),
			signal: AbortSignal.any([stopping.signal, AbortSignal.timeout(65_000)]),
		})
		if (!response.ok) throw new Error('Switchboard request failed')
		const body = await response.text()
		if (body.length > 1024 * 1024) throw new Error('Switchboard response was too large')
		const envelope = JSON.parse(body)
		if (envelope.error || !envelope.result) throw new Error('Switchboard rejected the request')
		return envelope.result
	}

	function remember(threadID: string) {
		if (!nativeID.test(threadID)) return
		threads.delete(threadID)
		threads.add(threadID)
		while (threads.size > 200) {
			const oldest = threads.values().next().value!
			threads.delete(oldest)
			sessions.delete(oldest)
		}
	}

	async function perform(command: Command) {
		// A delayed HTTP response must not execute an instruction after its
		// requester has already timed out. Claimed commands are never redelivered.
		if (stopped || !threads.has(command.thread_id) || command.expires <= Date.now() / 1000) return
		let result: Reply = { ok: false }
		try {
			const thread = amp.threads.get(command.thread_id as ThreadID)
			if (command.action === 'cancel') {
				await thread.cancel()
				const deadline = Math.min(Date.now() + 8000, command.expires * 1000)
				let state = await thread.state.get()
				while (!['idle', 'error'].includes(state) && Date.now() < deadline && !stopped) {
					await delay(100)
					state = await thread.state.get()
				}
				result = { ok: ['idle', 'error'].includes(state), state }
			} else if (command.action === 'steer' && typeof command.text === 'string' && command.text.trim()) {
				await thread.appendUserMessage({ type: 'user-message', content: command.text }, { steer: true })
				result = { ok: true }
			}
		} catch {
			// Error payloads can contain connection secrets; report only status.
		}
		acknowledgments.set(command.id, result)
	}

	async function acknowledge() {
		for (const [command_id, result] of acknowledgments) {
			await rpc('amp_ack', { command_id, client_id: clientID, result })
			acknowledgments.delete(command_id)
		}
	}

	async function loop() {
		while (!stopped) {
			try {
				await acknowledge()
				if (!threads.size) { await delay(500); continue }
				const response = await rpc('amp_poll', { thread_ids: [...threads], client_id: clientID, wait_seconds: 15 })
				for (const [id, session] of Object.entries(response.sessions || {})) sessions.set(id, session as PhoneSession)
				// Run separate threads' controls independently; cancellation may wait
				// for a tool to stop and should not hold up another phone session.
				await Promise.all((response.commands || []).map(perform))
				await acknowledge()
			} catch {
				if (stopped) break
				if (Date.now() - lastWarning > 60_000) {
					amp.logger.log('Switchboard connection is unavailable; phone controls are not acknowledged.')
					lastWarning = Date.now()
				}
				await delay(2000)
			}
		}
	}

	async function phoneSession(threadID: string): Promise<PhoneSession> {
		remember(threadID)
		if (sessions.has(threadID)) return sessions.get(threadID)!
		// The runner may see session.start just before the bridge persists the
		// stream's native ID. Wait briefly for that binding without making a new
		// phone session or claiming control work from the background poller.
		for (let attempt = 0; attempt < 10; attempt++) {
			const response = await rpc('amp_poll', {
				thread_ids: [threadID], client_id: clientID, wait_seconds: 0, lookup_only: true,
			})
			const session = response.sessions?.[threadID]
			if (session) { sessions.set(threadID, session); return session }
			await delay(250)
		}
		throw new Error('This thread has no Switchboard phone session. Start it from the phone or Switchboard first.')
	}

	amp.on('session.start', event => { remember(event.thread.id) })
	amp.on('agent.start', (_event, ctx) => { remember(ctx.thread.id) })
	amp.onDispose(() => { stopped = true; stopping.abort() })

	amp.registerTool({
		name: 'phone_ask_user',
		description: 'Ask the owner one clarification question on their Cisco desk phone, in this thread’s existing Switchboard session. A pending answer is not consent. Do not place duplicate questions; collect it with phone_get_answer.',
		inputSchema: {
			type: 'object', properties: {
				question: { type: 'string', maxLength: 3000 },
				options: { type: 'array', maxItems: 6, items: { type: 'string' } },
			}, required: ['question'], additionalProperties: false,
		},
		async execute(input, ctx) {
			const question = typeof input.question === 'string' ? input.question.trim() : ''
			const options = input.options || []
			if (!question || question.length > 3000 || !Array.isArray(options) || options.length > 6
				|| options.some((option: unknown) => typeof option !== 'string')) throw new Error('Provide one short phone question')
			const session = await phoneSession(ctx.thread.id)
			const record = await rpc('ask', {
				session_id: session.id, request_key: 'amp:' + ctx.thread.id + ':' + randomUUID(),
				questions: [{ id: 'answer', header: 'Question', question,
					options: options.map((label: string) => ({ label, description: '' })) }],
			})
			const answer = await rpc('question', { question_id: record.id, wait_seconds: 50 })
			return JSON.stringify({ question_id: record.id, session_id: session.id,
				state: answer.state, answers: answer.answers, extension: session.extension,
				instructions: 'Pending means no answer or approval. Use phone_get_answer to collect this same question.' })
		},
	})
	amp.registerTool({
		name: 'phone_get_answer',
		description: 'Collect the owner’s saved answer to an existing phone question. Waits up to 50 seconds and never rings again.',
		inputSchema: { type: 'object', properties: { question_id: { type: 'string' } }, required: ['question_id'], additionalProperties: false },
		async execute(input, ctx) {
			const session = await phoneSession(ctx.thread.id)
			const question_id = typeof input.question_id === 'string' ? input.question_id : ''
			if (!question_id) throw new Error('Provide the saved question_id')
			const existing = await rpc('question', { question_id, wait_seconds: 0 })
			if (existing.session_id !== session.id) throw new Error('That question belongs to another phone session')
			return JSON.stringify(await rpc('question', { question_id, wait_seconds: 50 }))
		},
	})
	void loop()
}
