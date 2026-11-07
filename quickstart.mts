import {
  assistantText,
  createZooworkClient,
  isRunFinished,
  runOutcome,
  toolCall,
} from '@zoowork-ai/sdk'

const client = createZooworkClient()

const agent = await client.createAgent({
  resource: { name: 'quickstart-agent' },
})
console.log(`Agent ID: ${agent.agent_id}`)

await client.startAgent(agent.agent_id)
await client.waitUntilRunning(agent.agent_id)

const session = await client.createSession(agent.agent_id, {})
console.log(`Session ID: ${session.session_id}`)

const receipt = await client.postEvents(agent.agent_id, session.session_id, [{
  type: 'user.message',
  content: 'Create report.md with a sales table: January $100, February $120, March $80. '
    + 'Include the total. Read the file to verify it, then report the total.',
}])
if (receipt.events[0]?.accepted !== true) throw new Error('Message was not accepted')

const controller = new AbortController()
let outcome: ReturnType<typeof runOutcome>

try {
  for await (const event of client.streamEvents(agent.agent_id, session.session_id, {
    signal: controller.signal,
  })) {
    process.stdout.write(assistantText(event))
    const call = toolCall(event)
    if (call?.phase === 'start') console.log(`\n[tool] ${call.toolName}`)

    if (isRunFinished(event)) {
      outcome = runOutcome(event)
      console.log(`\nTurn: ${outcome}`)
      break
    }
  }
} finally {
  controller.abort()
}
if (outcome !== 'succeeded') throw new Error(`Turn did not succeed: ${outcome ?? 'stream closed'}`)

await client.stopAgent(agent.agent_id)
await client.deleteAgent(agent.agent_id)
console.log('Cleaned up agent.')
