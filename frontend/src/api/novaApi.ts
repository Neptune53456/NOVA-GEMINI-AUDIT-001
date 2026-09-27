export const novaStates = [
  'idle', 'listening', 'thinking', 'responding', 'acting', 'awaiting-confirmation',
  'success', 'error', 'self-improving',
] as const

export type NovaState = (typeof novaStates)[number]
export interface NovaStateResponse { state: NovaState; label: string; busy: boolean; message: string }
export interface HealthResponse { status: 'ok'; service: 'nova-api'; version: '1' }
export type ComponentStatus = 'available' | 'degraded' | 'unavailable' | 'unknown'
export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected'
export interface ProjectStatus { git_available: boolean; branch: string | null; clean: boolean | null; modified_count: number | null; untracked_count: number | null }
export interface CockpitComponent { id: string; label: string; status: ComponentStatus; message: string }
export interface ValidationSummary { status: 'passed' | 'failed' | 'unknown'; generated_at: string | null; passed: number | null; failed: number | null; warnings: number | null; message: string }
export interface CockpitAlert { level: 'info' | 'warning' | 'error'; message: string }
export interface CockpitResponse {
  generated_at: string
  api: { status: 'ok'; uptime_seconds: number }
  nova: NovaStateResponse
  project: ProjectStatus
  components: CockpitComponent[]
  validation: ValidationSummary
  alerts: CockpitAlert[]
}
export interface ProjectBrainStatus { status: 'ready' | 'unavailable'; file_count: number; symbol_count: number; last_updated: string | null; estimated_size: number }
export type ConversationMode = 'supervised' | 'autonomous-local'
export interface ConversationMessage { message_id: string; role: 'user' | 'assistant'; content: string; created_at: string }
export interface ConversationSummary { conversation_id: string; created_at: string; status: 'idle' | 'thinking' | 'acting' | 'awaiting-confirmation' | 'responding' | 'success' | 'error' | 'cancelled'; mode: ConversationMode }
export interface SendMessageResponse { user_message: ConversationMessage; assistant_message: ConversationMessage; status: 'success' }
export type GenerationStatus = 'connecting' | 'thinking' | 'acting' | 'awaiting-confirmation' | 'responding' | 'success' | 'error' | 'cancelled'
export interface ConfirmationCard { token: string; capability_id: string; path?: string; effect: string; expires_in_seconds: number; risk?: string; reversible?: boolean; goal_id?: string }
export interface GoalStep { status: string; capability_id?: string; risk?: string; confirmation?: ConfirmationCard }
export interface GoalResponse { goal_id?: string; status: string; objective?: string; error?: string; message?: string; plan: GoalStep[] }
export interface MissionProgress { mission_id: string; mission_state: 'pending' | 'running' | 'awaiting_confirmation' | 'paused' | 'completed' | 'failed' | 'cancelled'; step_index: number; step_count: number; capability_id?: string; error_category?: string }
export type GenerationEvent =
  | { event: 'generation.started'; generation_id: string; status: 'thinking' }
  | { event: 'generation.delta'; generation_id: string; status: 'responding'; delta: string }
  | { event: 'generation.completed'; generation_id: string; status: 'success'; completion_state?: 'completed_verified' | 'completed_unverified'; warning?: string; user_message: ConversationMessage; assistant_message: ConversationMessage }
  | { event: 'generation.error'; generation_id: string; status: 'error'; error: string; code?: string }
  | { event: 'generation.cancelled'; generation_id: string; status: 'cancelled' }
  | { event: 'generation.state'; generation_id: string; status: 'acting' | 'thinking'; capability_id?: string }
  | { event: 'generation.awaiting_confirmation'; generation_id: string; status: 'awaiting-confirmation'; user_message: ConversationMessage; confirmation: ConfirmationCard; goal_id?: string }
  | { event: 'goal.awaiting_confirmation'; goal_id?: string; generation_id?: string }
  | ({ event: 'mission.created' | 'mission.started' | 'mission.step.started' | 'mission.step.completed' | 'mission.awaiting_confirmation' | 'mission.paused' | 'mission.completed' | 'mission.failed' | 'mission.cancelled'; generation_id: string; status: GenerationStatus } & MissionProgress & { confirmation?: ConfirmationCard; user_message?: ConversationMessage; goal_id?: string })
export const MAX_MESSAGE_LENGTH = 8_000
const CONVERSATION_POST_TIMEOUT_MS = 65_000

const stateValues = new Set<string>(novaStates)
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const isNovaState = (value: unknown): value is NovaStateResponse => isRecord(value) && typeof value.state === 'string' && stateValues.has(value.state) && typeof value.label === 'string' && typeof value.busy === 'boolean' && typeof value.message === 'string'
const isHealth = (value: unknown): value is HealthResponse => isRecord(value) && value.status === 'ok' && value.service === 'nova-api' && value.version === '1'
const componentValues = new Set(['available', 'degraded', 'unavailable', 'unknown'])
const isNullableNumber = (value: unknown) => value === null || typeof value === 'number'
const isCockpit = (value: unknown): value is CockpitResponse => {
  if (!isRecord(value) || typeof value.generated_at !== 'string' || !isRecord(value.api) || value.api.status !== 'ok' || typeof value.api.uptime_seconds !== 'number' || !isNovaState(value.nova)) return false
  if (!isRecord(value.project) || typeof value.project.git_available !== 'boolean' || !(value.project.branch === null || typeof value.project.branch === 'string') || !(value.project.clean === null || typeof value.project.clean === 'boolean') || !isNullableNumber(value.project.modified_count) || !isNullableNumber(value.project.untracked_count)) return false
  if (!Array.isArray(value.components) || !value.components.every((item) => isRecord(item) && typeof item.id === 'string' && typeof item.label === 'string' && typeof item.status === 'string' && componentValues.has(item.status) && typeof item.message === 'string')) return false
  if (!isRecord(value.validation) || !['passed', 'failed', 'unknown'].includes(String(value.validation.status)) || !(value.validation.generated_at === null || typeof value.validation.generated_at === 'string') || !isNullableNumber(value.validation.passed) || !isNullableNumber(value.validation.failed) || !isNullableNumber(value.validation.warnings) || typeof value.validation.message !== 'string') return false
  return Array.isArray(value.alerts) && value.alerts.every((item) => isRecord(item) && ['info', 'warning', 'error'].includes(String(item.level)) && typeof item.message === 'string')
}

async function getJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timeout = window.setTimeout(abort, 3_000)
  try {
    const response = await fetch(path, { method: 'GET', headers: { Accept: 'application/json' }, signal: controller.signal })
    if (!response.ok) throw new Error('Nova API request failed')
    return await response.json() as unknown
  } finally {
    window.clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}
export async function getGodEyes(path: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  const value = await getJson(`/api/v1/god-eye/${path}`, signal)
  if (!isRecord(value)) throw new Error('Invalid God Eyes response')
  return value
}

export type GodEyesEndpoint = 'health' | 'market' | 'events' | 'forecasts' | 'opportunities' | 'portfolio' | 'walk-forward' | 'experiments' | 'candidates' | 'incumbent' | 'live-forward' | 'alternative-data/health' | 'alerts' | 'governance' | 'trader/status' | 'endurance/health'
export async function getBusinessData(path: GodEyesEndpoint, signal?: AbortSignal): Promise<Record<string, unknown>> { return getGodEyes(path, signal) }

export type MarketTimeframe = '1m' | '5m' | '1h' | '1d'
export interface InstrumentDataSet {
  history: Record<string, unknown>; market: Record<string, unknown>; forecasts: Record<string, unknown>
  events: Record<string, unknown>; patterns: Record<string, unknown>; markers: Record<string, unknown>
}
export async function getInstrumentData(instrument: string, timeframe: MarketTimeframe, signal?: AbortSignal): Promise<InstrumentDataSet> {
  const symbol=encodeURIComponent(instrument); const shared=`instrument=${symbol}&limit=500`
  const [history,market,forecasts,events,patterns,markers]=await Promise.all([
    getGodEyes(`history/${symbol}?interval=${timeframe}&limit=1000`,signal),getGodEyes(`market?symbol=${symbol}`,signal),
    getGodEyes(`forecasts?${shared}`,signal),getGodEyes(`events?${shared}`,signal),getGodEyes(`patterns?${shared}`,signal),
    getGodEyes(`trader/markers/${symbol}?limit=500`,signal),
  ])
  return {history,market,forecasts,events,patterns,markers}
}
export async function getTradeDetail(tradeId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
  return getGodEyes(`trader/trades/${encodeURIComponent(tradeId)}`,signal)
}

async function apiJson(path: string, method: 'POST' | 'DELETE', body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const controller = method === 'POST' ? new AbortController() : undefined
  const abort = () => controller?.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timeout = controller === undefined ? undefined : window.setTimeout(() => controller.abort(), CONVERSATION_POST_TIMEOUT_MS)
  try {
    const response = await fetch(path, { method, headers: body === undefined ? undefined : { 'Content-Type': 'application/json', Accept: 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body), signal: controller?.signal })
    if (!response.ok) {
      let message = 'La requête Nova a échoué.'
      try { const value = await response.json() as { detail?: unknown }; if (typeof value.detail === 'string') message = value.detail } catch { /* public fallback */ }
      throw new Error(message)
    }
    return response.status === 204 ? undefined : response.json() as Promise<unknown>
  } catch (error) {
    if (controller?.signal.aborted) throw new Error('Nova met trop de temps à répondre. Veuillez réessayer.')
    throw error
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout)
    signal?.removeEventListener('abort', abort)
  }
}

export async function createConversation(mode: ConversationMode): Promise<ConversationSummary> {
  return await apiJson('/api/v1/conversations', 'POST', { mode }) as ConversationSummary
}

export async function sendConversationMessage(id: string, content: string): Promise<SendMessageResponse> {
  return await apiJson(`/api/v1/conversations/${encodeURIComponent(id)}/messages`, 'POST', { content }) as SendMessageResponse
}

export async function streamConversationMessage(
  id: string,
  content: string,
  onEvent: (event: GenerationEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/v1/conversations/${encodeURIComponent(id)}/messages/stream`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ content }), signal,
  })
  if (!response.ok) {
    let message = 'La requête Nova a échoué.'
    try { const value = await response.json() as { detail?: unknown }; if (typeof value.detail === 'string') message = value.detail } catch { /* public fallback */ }
    throw new Error(message)
  }
  if (!response.body) throw new Error('Le flux Nova est indisponible.')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const frames = buffer.split('\n\n'); buffer = frames.pop() ?? ''
    for (const frame of frames) {
      const data = frame.split('\n').find((line) => line.startsWith('data: '))?.slice(6)
      if (!data) continue
      const parsed = JSON.parse(data) as unknown
      if (!isRecord(parsed)) throw new Error('Événement Nova invalide.')
      const eventName = typeof parsed.event === 'string' ? parsed.event : parsed.type
      if (typeof eventName !== 'string') throw new Error('Événement Nova invalide.')
      if (eventName !== 'goal.awaiting_confirmation' && typeof parsed.generation_id !== 'string') throw new Error('Événement Nova invalide.')
      onEvent({ ...parsed, event: eventName } as GenerationEvent)
    }
    if (done) break
  }
}

export async function cancelGeneration(conversationId: string, generationId: string): Promise<void> {
  await apiJson(`/api/v1/conversations/${encodeURIComponent(conversationId)}/generations/${encodeURIComponent(generationId)}/cancel`, 'POST')
}

export async function decideConfirmation(conversationId: string, token: string, approved: boolean): Promise<{ status: 'success' | 'refused'; assistant_message: ConversationMessage }> {
  return await apiJson(`/api/v1/conversations/${encodeURIComponent(conversationId)}/confirmations`, 'POST', { token, approved }) as { status: 'success' | 'refused'; assistant_message: ConversationMessage }
}

export async function getGoal(goalId: string, signal?: AbortSignal): Promise<GoalResponse> {
  return await getJson(`/api/v1/goals/${encodeURIComponent(goalId)}`, signal) as GoalResponse
}

export async function resumeGoal(goalId: string, token: string, approved: boolean): Promise<void> {
  await apiJson(`/api/v1/goals/${encodeURIComponent(goalId)}/resume`, 'POST', { token, approved })
}

export async function deleteConversation(id: string): Promise<void> {
  await apiJson(`/api/v1/conversations/${encodeURIComponent(id)}`, 'DELETE')
}

export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const value = await getJson('/api/v1/health', signal)
  if (!isHealth(value)) throw new Error('Invalid Nova health response')
  return value
}

export async function getNovaState(signal?: AbortSignal): Promise<NovaStateResponse> {
  const value = await getJson('/api/v1/state', signal)
  if (!isNovaState(value)) throw new Error('Invalid Nova state response')
  return value
}

export async function getCockpit(signal?: AbortSignal): Promise<CockpitResponse> {
  const value = await getJson('/api/v1/cockpit', signal)
  if (!isCockpit(value)) throw new Error('Invalid Nova cockpit response')
  return value
}

export async function getProjectBrain(signal?: AbortSignal): Promise<ProjectBrainStatus> {
  const value = await getJson('/api/v1/project-brain', signal)
  if (!isRecord(value) || !['ready', 'unavailable'].includes(String(value.status)) || typeof value.file_count !== 'number' || typeof value.symbol_count !== 'number' || !(value.last_updated === null || typeof value.last_updated === 'string') || typeof value.estimated_size !== 'number') throw new Error('Invalid Project Brain response')
  return { status: value.status as ProjectBrainStatus['status'], file_count: value.file_count,
    symbol_count: value.symbol_count, last_updated: value.last_updated, estimated_size: value.estimated_size }
}
