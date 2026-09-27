import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Icon } from '../../components/Icon'
import { cancelGeneration, createConversation, deleteConversation, getGoal, MAX_MESSAGE_LENGTH, resumeGoal, streamConversationMessage, type ConfirmationCard, type ConversationMessage, type ConversationMode, type GenerationStatus, type GoalResponse, type MissionProgress } from '../../api/novaApi'
import { useNovaState } from '../../state/NovaStateContext'
import { GoalConfirmationCard } from './GoalConfirmationCard'
import { projectGoalUi, type GoalUiStatus } from '../../state/goalStateProjection'
import { generationErrorMessage, projectsVerifiedSuccess, shouldProjectGenerationError } from '../../state/generationErrorProjection'

type DisplayMessage = ConversationMessage & { failed?: boolean }

export function ConversationPage() {
  const { refresh } = useNovaState()
  const [conversationId, setConversationId] = useState<string>()
  const [messages, setMessages] = useState<DisplayMessage[]>([])
  const [content, setContent] = useState('')
  const [mode, setMode] = useState<ConversationMode>('supervised')
  const [generationStatus, setGenerationStatus] = useState<GenerationStatus>()
  const [generationId, setGenerationId] = useState<string>()
  const [error, setError] = useState<string>()
  const [retryContent, setRetryContent] = useState<string>()
  const [confirmation, setConfirmation] = useState<ConfirmationCard>()
  const [confirmationPending, setConfirmationPending] = useState(false)
  const [confirmationExpired, setConfirmationExpired] = useState(false)
  const [goalId, setGoalId] = useState<string>()
  const [goalStatus, setGoalStatus] = useState<string>()
  const [mission, setMission] = useState<MissionProgress>()
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | undefined>(undefined)
  const activeRef = useRef<{ conversationId?: string; generationId?: string; run: number }>({ run: 0 })
  const associatedGoalRef = useRef<string | undefined>(undefined)
  const loadingConfirmationGoalsRef = useRef(new Set<string>())
  const shownConfirmationRef = useRef<string | undefined>(undefined)
  const terminalSuccessRef = useRef(false)
  const goalInProgress = goalStatus === 'acting' || goalStatus === 'verifying' || goalStatus === 'running'
  const busy = generationStatus !== undefined || confirmationPending || goalInProgress

  function applyGoal(goal: GoalResponse, knownGoalId?: string) {
    const resolvedGoalId = goal.goal_id ?? knownGoalId
    if (resolvedGoalId) { associatedGoalRef.current = resolvedGoalId; setGoalId(resolvedGoalId) }
    setGoalStatus(goal.status)
    const pendingStep = goal.plan.find((step) => step.status === 'awaiting_confirmation' && step.confirmation)
    const projection = projectGoalUi(goal.status as GoalUiStatus)
    if (projection.confirmationVisible && pendingStep?.confirmation) {
      if (shownConfirmationRef.current === pendingStep.confirmation.token) return
      shownConfirmationRef.current = pendingStep.confirmation.token
      setConfirmation({ ...pendingStep.confirmation, risk: pendingStep.confirmation.risk ?? pendingStep.risk, goal_id: resolvedGoalId })
      setConfirmationExpired(false)
    } else if (!projection.confirmationVisible) setConfirmation(undefined)
    if (goal.status === 'completed_verified') {
      setMission((current) => current ? { ...current, mission_state: 'completed' } : current)
      setError(undefined)
    }
    if (goal.status === 'blocked' || goal.status === 'failed') {
      setMission((current) => current ? { ...current, mission_state: 'failed' } : current)
      setError(goal.error ?? goal.message ?? 'L’objectif n’a pas pu être terminé.')
    }
  }

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, busy])
  useEffect(() => () => {
    const active = activeRef.current
    abortRef.current?.abort()
    if (active.conversationId && active.generationId) void cancelGeneration(active.conversationId, active.generationId).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!goalId || !goalInProgress) return
    let stopped = false
    const controller = new AbortController()
    const poll = async () => {
      try {
        const goal = await getGoal(goalId, controller.signal)
        if (!stopped) applyGoal(goal, goalId)
      } catch (reason) {
        if (!stopped && !(reason instanceof DOMException && reason.name === 'AbortError')) setError(reason instanceof Error ? reason.message : 'Actualisation de l’objectif impossible.')
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1_500)
    return () => { stopped = true; controller.abort(); window.clearInterval(timer) }
  }, [goalId, goalInProgress])

  async function submit(raw = content) {
    const trimmed = raw.trim()
    if (!trimmed || busy || trimmed.length > MAX_MESSAGE_LENGTH) return
    const run = activeRef.current.run + 1
    activeRef.current = { run }
    associatedGoalRef.current = undefined; shownConfirmationRef.current = undefined; loadingConfirmationGoalsRef.current.clear(); terminalSuccessRef.current = false
    setGenerationStatus('connecting'); setError(undefined); setRetryContent(undefined); setGoalId(undefined); setGoalStatus(undefined); setConfirmation(undefined); setConfirmationExpired(false)
    const optimistic: DisplayMessage = { message_id: `local-${crypto.randomUUID()}`, role: 'user', content: trimmed, created_at: new Date().toISOString() }
    const draftId = `draft-${crypto.randomUUID()}`
    setMessages((current) => [...current, optimistic]); setContent('')
    try {
      let id = conversationId
      if (!id) { const created = await createConversation(mode); id = created.conversation_id; if (activeRef.current.run !== run) return; setConversationId(id) }
      activeRef.current.conversationId = id
      const controller = new AbortController(); abortRef.current = controller
      await streamConversationMessage(id, trimmed, (event) => {
        if (activeRef.current.run !== run) return
        if (event.generation_id) { activeRef.current.generationId = event.generation_id; setGenerationId(event.generation_id) }
        if (event.event === 'goal.awaiting_confirmation') {
          const linkedGoalId = event.goal_id ?? associatedGoalRef.current
          if (!linkedGoalId || loadingConfirmationGoalsRef.current.has(linkedGoalId)) return
          loadingConfirmationGoalsRef.current.add(linkedGoalId)
          associatedGoalRef.current = linkedGoalId; setGoalId(linkedGoalId); setGoalStatus('awaiting_confirmation'); setGenerationStatus(undefined)
          void getGoal(linkedGoalId).then((goal) => {
            if (activeRef.current.run === run) applyGoal(goal, linkedGoalId)
          }).catch((reason: unknown) => {
            if (activeRef.current.run === run) setError(reason instanceof Error ? reason.message : 'Actualisation de l’objectif impossible.')
          }).finally(() => loadingConfirmationGoalsRef.current.delete(linkedGoalId))
          return
        }
        if (event.event === 'generation.started') setGenerationStatus('thinking')
        if (event.event.startsWith('mission.')) {
          const missionEvent = event as MissionProgress & { confirmation?: ConfirmationCard; user_message?: ConversationMessage; goal_id?: string }
          setMission({ mission_id: missionEvent.mission_id, mission_state: missionEvent.mission_state, step_index: missionEvent.step_index, step_count: missionEvent.step_count, capability_id: missionEvent.capability_id, error_category: missionEvent.error_category })
          if (event.event === 'mission.awaiting_confirmation' && missionEvent.confirmation) {
            if (missionEvent.user_message) setMessages((current) => [...current.filter((message) => message.message_id !== optimistic.message_id && message.message_id !== draftId), missionEvent.user_message!])
            const linkedGoalId = missionEvent.goal_id ?? missionEvent.confirmation.goal_id
            if (linkedGoalId) associatedGoalRef.current = linkedGoalId
            shownConfirmationRef.current = missionEvent.confirmation.token
            setGoalId(linkedGoalId); setGoalStatus('awaiting_confirmation'); setConfirmation({ ...missionEvent.confirmation, goal_id: linkedGoalId }); setConfirmationExpired(false); setGenerationStatus(undefined)
          }
        }
        if (event.event === 'generation.state') setGenerationStatus(event.status)
        if (event.event === 'generation.delta') {
          setGenerationStatus('responding')
          setMessages((current) => {
            const draft = current.find((message) => message.message_id === draftId)
            if (draft) return current.map((message) => message.message_id === draftId ? { ...message, content: message.content + event.delta } : message)
            return [...current, { message_id: draftId, role: 'assistant', content: event.delta, created_at: new Date().toISOString() }]
          })
        }
        if (event.event === 'generation.completed') {
          terminalSuccessRef.current = true
          if (projectsVerifiedSuccess(event.completion_state)) setGoalStatus('completed_verified')
          setError(undefined)
          setMessages((current) => [...current.filter((message) => message.message_id !== optimistic.message_id && message.message_id !== draftId), event.user_message, event.assistant_message])
          setGenerationStatus(undefined)
        }
        if (event.event === 'generation.error') {
          if (!shouldProjectGenerationError(terminalSuccessRef.current)) return
          setMessages((current) => current.filter((message) => message.message_id !== draftId).map((message) => message.message_id === optimistic.message_id ? { ...message, failed: true } : message))
          setError(generationErrorMessage(event.code, event.error)); setRetryContent(trimmed); setGenerationStatus(undefined)
        }
        if (event.event === 'generation.cancelled') {
          setMessages((current) => current.filter((message) => message.message_id !== optimistic.message_id && message.message_id !== draftId))
          setGenerationStatus(undefined)
        }
        if (event.event === 'generation.awaiting_confirmation') {
          setMessages((current) => [...current.filter((message) => message.message_id !== optimistic.message_id && message.message_id !== draftId), event.user_message])
          const linkedGoalId = event.goal_id ?? event.confirmation.goal_id
          if (linkedGoalId) associatedGoalRef.current = linkedGoalId
          shownConfirmationRef.current = event.confirmation.token
          setGoalId(linkedGoalId); setGoalStatus('awaiting_confirmation'); setConfirmation({ ...event.confirmation, goal_id: linkedGoalId }); setConfirmationExpired(false); setGenerationStatus(undefined)
        }
      }, controller.signal)
    } catch (reason) {
      if (activeRef.current.run !== run) return
      if (!(reason instanceof DOMException && reason.name === 'AbortError')) {
        setMessages((current) => current.map((message) => message.message_id === optimistic.message_id ? { ...message, failed: true } : message))
        setError(reason instanceof Error ? reason.message : 'Moteur Nova indisponible.'); setRetryContent(trimmed)
      }
    } finally {
      if (activeRef.current.run === run) {
        activeRef.current = { run }; abortRef.current = undefined; setGenerationStatus(undefined); setGenerationId(undefined)
        await refresh(); window.setTimeout(() => inputRef.current?.focus(), 0)
      }
    }
  }

  async function stop() {
    const active = activeRef.current
    if (!active.conversationId || !active.generationId) { abortRef.current?.abort(); return }
    setGenerationStatus('cancelled')
    try { await cancelGeneration(active.conversationId, active.generationId) }
    catch { setError('L’annulation n’a pas pu être confirmée par Nova.'); abortRef.current?.abort() }
  }

  async function newConversation() {
    if (messages.length > 0 && !window.confirm('Effacer cette discussion temporaire ?')) return
    const previousId = conversationId
    if (busy) { activeRef.current.run += 1; void stop(); abortRef.current?.abort() }
    else if (previousId) {
      try { await deleteConversation(previousId) } catch (reason) { setError(reason instanceof Error ? reason.message : 'Suppression impossible.'); return }
    }
    associatedGoalRef.current = undefined; shownConfirmationRef.current = undefined; loadingConfirmationGoalsRef.current.clear()
    setConversationId(undefined); setMessages([]); setError(undefined); setRetryContent(undefined); setConfirmation(undefined); setConfirmationExpired(false); setGoalId(undefined); setGoalStatus(undefined); setMission(undefined); setGenerationStatus(undefined); setGenerationId(undefined); inputRef.current?.focus()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void submit() } }
  function retry() { if (retryContent) { setMessages((current) => current.filter((message) => !message.failed)); void submit(retryContent) } }
  async function confirm(approved: boolean) {
    const linkedGoalId = goalId ?? confirmation?.goal_id
    if (!confirmation || confirmationPending) return
    if (!linkedGoalId) { setError('Cette confirmation n’est pas liée à un objectif Nova actualisable.'); return }
    setConfirmationPending(true); setGoalStatus(approved ? 'acting' : 'running'); setError(undefined)
    try {
      await resumeGoal(linkedGoalId, confirmation.token, approved)
      applyGoal(await getGoal(linkedGoalId), linkedGoalId)
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Confirmation impossible.'
      if (/expir/i.test(message)) setConfirmationExpired(true)
      setGoalStatus('awaiting_confirmation'); setError(message)
    } finally { setConfirmationPending(false); await refresh() }
  }
  const statusLabel = goalStatus === 'verifying' ? 'Vérification après action…' : goalInProgress ? 'Nova agit…' : generationStatus === 'connecting' ? 'Connexion…' : generationStatus === 'thinking' ? 'Réflexion en cours…' : generationStatus === 'acting' ? 'Nova agit…' : generationStatus === 'responding' ? 'Réponse en cours…' : 'Annulation…'

  return <section className="conversation-page">
    <header className="conversation-header"><div><p className="eyebrow">SESSION TEMPORAIRE</p><h1>Conversation</h1></div><button className="ghost-button" type="button" onClick={() => void newConversation()}><Icon name="plus" size={17} /><span>Nouvelle discussion</span></button></header>
    <p className="conversation-notice">Historique conservé uniquement jusqu’à l’arrêt de Nova. Le mode autonome local est une préférence et n’autorise encore aucune action sur l’ordinateur.</p>
    <div className="mode-row"><label htmlFor="conversation-mode">Mode</label><select id="conversation-mode" value={mode} disabled={Boolean(conversationId) || busy} onChange={(event) => setMode(event.target.value as ConversationMode)}><option value="supervised">Supervisé</option><option value="autonomous-local">Autonome local (préparé, sans actions)</option></select></div>
    {mission && <div className={`mission-status mission-${mission.mission_state}`} role="status"><strong>Mission</strong><span>{mission.mission_state === 'awaiting_confirmation' ? 'En attente de confirmation' : mission.mission_state === 'paused' ? 'En pause' : mission.mission_state === 'completed' ? 'Terminée' : mission.mission_state === 'failed' ? 'Échec' : mission.mission_state === 'cancelled' ? 'Annulée' : 'En cours'} · étape {Math.min(mission.step_index + 1, mission.step_count)} / {mission.step_count}</span></div>}
    <div className="messages" aria-live="polite" aria-busy={busy}>
      {messages.length === 0 && <p className="empty-conversation">Envoyez un message pour créer une discussion avec le moteur Nova.</p>}
      {messages.map((message) => <div key={message.message_id} className={`message ${message.role === 'assistant' ? 'nova-message' : 'user-message'} ${message.failed ? 'failed-message' : ''}`}>{message.role === 'assistant' && <span className="avatar">N</span>}<div><span className="message-author">{message.role === 'assistant' ? 'NOVA' : 'VOUS'} <time>{new Date(message.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time></span><p>{message.content}</p>{message.failed && <small>Envoi non abouti</small>}</div></div>)}
      {confirmation && <GoalConfirmationCard key={confirmation.token} confirmation={confirmation} pending={confirmationPending} expired={confirmationExpired} onDecision={(approved) => void confirm(approved)} />}
      {goalStatus === 'completed_verified' && <div className="goal-result goal-success" role="status"><Icon name="shield" size={18} /><div><strong>Objectif terminé et vérifié</strong><span>L’action a été exécutée puis contrôlée par Nova.</span></div></div>}
      {busy && !messages.some((message) => message.message_id.startsWith('draft-')) && <div className="message nova-message thinking-message"><span className="avatar">N</span><div><span className="message-author">NOVA</span><p>{statusLabel}</p></div></div>}
      <div ref={endRef} />
    </div>
    <div className="composer-area">{error && <div className="conversation-error" role="alert"><span>{error}</span>{retryContent && <button type="button" disabled={busy} onClick={retry}>Réessayer</button>}</div>}<div className="composer"><textarea ref={inputRef} aria-label="Message" placeholder="Écrivez à Nova…" rows={2} maxLength={MAX_MESSAGE_LENGTH} value={content} disabled={busy} onChange={(event) => setContent(event.target.value)} onKeyDown={onKeyDown} />{busy ? <button type="button" className="stop-button" disabled={!generationId || generationStatus === 'cancelled'} onClick={() => void stop()}>Arrêter</button> : <button type="button" className="send-button" aria-label="Envoyer" disabled={!content.trim()} onClick={() => void submit()}><Icon name="arrow" /></button>}</div><p className="composer-hint">{content.length.toLocaleString()} / {MAX_MESSAGE_LENGTH.toLocaleString()} caractères · Entrée pour envoyer · Maj+Entrée pour une nouvelle ligne</p></div>
  </section>
}
