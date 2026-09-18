/**
 * Rolling 1-on-1 Agenda — Interactive Frontend Application Logic
 */

document.addEventListener('DOMContentLoaded', () => {
  // Application State — reportUserId/actingUserId/actingRole/userSecret
  // are populated by a real login (POST /webhooks/login), not hardcoded.
  // sharedToken is the shared "trusted internal caller" credential (not
  // per-user identity — see login's own docstring): injected server-side
  // into window.__AGENDA_TOKEN__ by GET /ui (app/fast_api_app.py), from
  // this process's own real AGENDA_WEBHOOK_TOKEN env var — never
  // hardcoded here, so it isn't sitting in this public source file.
  const state = {
    reportUserId: null,
    actingUserId: null,
    actingRole: null,
    sharedToken: window.__AGENDA_TOKEN__ || '',
    userSecret: null,
    displayName: null,
    loginEmail: null,
    // Reports this user manages (from POST /webhooks/login's
    // managed_reports) — used only to populate the Rolling Agenda tab's
    // own report picker. Every user always has exactly one dashboard
    // (their own); this never affects which dashboard is entered.
    managedReports: [],
    agendaItems: [],
    staleItems: [],
    pollInterval: null,
    isSyncing: false,
    selectedVisibility: 'shared',
    notificationsSource: null,
    currentPage: 'agenda',
    lastGraphDataJson: null
  };

  // Agent Chat state — talks directly to ADK's own /run endpoint against
  // this process's root_agent (app/agent.py), completely separate from the
  // agenda webhooks above (no X-Agenda-Token, no login identity). userId
  // 'user' is ADK's own dev-UI default (_ADK_DEV_UI_DEFAULT_USER_ID in
  // app/tools/custom_tools.py) — get_morning_pulse special-cases exactly
  // that id to fall back to WEBHOOK_OWNER_USER_ID, matching what `adk web`
  // does, so this chat behaves identically to the ADK playground.
  const chatState = {
    appName: 'app',
    userId: 'user',
    sessionId: null,
    sending: false
  };

  const SESSION_STORAGE_KEY = 'rollingAgendaSession';

  // Mock initial demo items for fallback visual rendering
  const mockInitialItems = [
    {
      id: 'item-101',
      text: 'Review system design doc for identity resolution service',
      source: 'manual',
      source_link: null,
      visibility: 'shared',
      status: 'open',
      surfaced_count: 2,
      created_by_role: 'report'
    },
    {
      id: 'item-102',
      text: 'JIRA-402: Auth token refresh race condition on mobile client',
      source: 'jira',
      source_link: 'https://jira.internal/browse/JIRA-402',
      visibility: 'shared',
      status: 'open',
      surfaced_count: 1,
      created_by_role: 'manager'
    },
    {
      id: 'item-103',
      text: 'Slack Question: Proposal for weekly asynchronous pulse updates',
      source: 'slack',
      source_link: 'https://slack.com/archives/C123/p456',
      visibility: 'shared',
      status: 'open',
      surfaced_count: 3,
      created_by_role: 'report'
    }
  ];

  const mockStaleItems = [
    {
      id: 'item-stale-1',
      text: 'Quarterly career growth framework and mentorship goal alignment',
      source: 'manual',
      source_link: null,
      visibility: 'shared',
      status: 'pending_consent',
      surfaced_count: 5,
      created_by_role: 'manager'
    }
  ];

  const loginOverlay = document.getElementById('login-overlay');
  const appContainer = document.getElementById('app-container');
  const formLogin = document.getElementById('form-login');
  const loginEmailInput = document.getElementById('login-email');
  const loginSecretInput = document.getElementById('login-secret');
  const btnLogin = document.getElementById('btn-login');
  const loginBtnText = document.getElementById('login-btn-text');
  const loginSpinner = document.getElementById('login-spinner');
  const loginErrorEl = document.getElementById('login-error');
  const loginView = document.getElementById('login-view');
  const requestSetupView = document.getElementById('request-setup-view');
  const setupCompleteView = document.getElementById('setup-complete-view');
  const linkRequestSetup = document.getElementById('link-request-setup');
  const linkBackToLogin = document.getElementById('link-back-to-login');
  const formRequestSetup = document.getElementById('form-request-setup');
  const setupRequestEmailInput = document.getElementById('setup-request-email');
  const btnRequestSetup = document.getElementById('btn-request-setup');
  const requestSetupBtnText = document.getElementById('request-setup-btn-text');
  const requestSetupSpinner = document.getElementById('request-setup-spinner');
  const requestSetupSuccess = document.getElementById('request-setup-success');
  const setupSecretBox = document.getElementById('setup-secret-box');
  const btnCopySecret = document.getElementById('btn-copy-secret');
  const btnContinueToLogin = document.getElementById('btn-continue-to-login');
  const accountLabel = document.getElementById('account-label');
  const pairSwitcher = document.getElementById('pair-switcher');
  const agendaReportPickerRowEl = document.getElementById('agenda-report-picker-row');
  const btnLogout = document.getElementById('btn-logout');
  const btnPreferences = document.getElementById('btn-preferences');
  const preferencesOverlay = document.getElementById('preferences-overlay');
  const formPreferences = document.getElementById('form-preferences');
  const prefTzInput = document.getElementById('pref-tz');
  const prefPulseTimeInput = document.getElementById('pref-pulse-time');
  const prefCutoffTimeInput = document.getElementById('pref-cutoff-time');
  const preferencesError = document.getElementById('preferences-error');
  const preferencesSuccess = document.getElementById('preferences-success');
  const btnPreferencesCancel = document.getElementById('btn-preferences-cancel');
  const btnPreferencesSave = document.getElementById('btn-preferences-save');
  const preferencesSaveBtnText = document.getElementById('preferences-save-btn-text');
  const preferencesSaveSpinner = document.getElementById('preferences-save-spinner');
  const googleConnectLoadingEl = document.getElementById('google-connect-loading');
  const googleConnectDisconnectedEl = document.getElementById('google-connect-disconnected');
  const googleConnectConnectedEl = document.getElementById('google-connect-connected');
  const googleConnectEmailEl = document.getElementById('google-connect-email');
  const linkGoogleConnect = document.getElementById('link-google-connect');
  const btnGoogleDisconnect = document.getElementById('btn-google-disconnect');
  const agendaListEl = document.getElementById('agenda-items-list');
  const staleSectionEl = document.getElementById('stale-section');
  const staleListEl = document.getElementById('stale-items-list');
  const itemCountPill = document.getElementById('item-count-pill');
  const emptyStateEl = document.getElementById('empty-state');
  const btnRefresh = document.getElementById('btn-refresh');
  const formAddNote = document.getElementById('form-add-note');
  const noteTextInput = document.getElementById('note-text');
  const noteVisibilityHidden = document.getElementById('note-visibility');
  const formTriggerMeeting = document.getElementById('form-trigger-meeting');
  const meetingTranscriptInput = document.getElementById('meeting-transcript-input');
  const btnRunMeeting = document.getElementById('btn-run-meeting');
  const btnStopMeeting = document.getElementById('btn-stop-meeting');
  const meetingBtnText = document.getElementById('meeting-btn-text');
  const meetingSpinner = document.getElementById('meeting-spinner');
  const pipelineStatusBox = document.getElementById('pipeline-status-box');
  const pipelineStatusText = document.getElementById('pipeline-status-text');
  const btnSimJira = document.getElementById('btn-sim-jira');
  const btnSimSlack = document.getElementById('btn-sim-slack');
  const btnSimAccomplishment = document.getElementById('btn-sim-accomplishment');
  const statusPill = document.getElementById('status-pill');
  const logEntriesEl = document.getElementById('log-entries');
  const logEmptyState = document.getElementById('log-empty-state');
  const btnClearLog = document.getElementById('btn-clear-log');
  const charCounter = document.getElementById('char-counter');
  const hintDot = document.getElementById('hint-dot');
  const backendStatusText = document.getElementById('backend-status-text');
  const goalsListEl = document.getElementById('goals-list');
  const goalsEmptyStateEl = document.getElementById('goals-empty-state');
  const careerGoalsListEl = document.getElementById('career-goals-list');
  const careerGoalsEmptyStateEl = document.getElementById('career-goals-empty-state');
  const notesListEl = document.getElementById('notes-list');
  const notesEmptyStateEl = document.getElementById('notes-empty-state');
  const dossiersListEl = document.getElementById('dossiers-list');
  const dossiersEmptyStateEl = document.getElementById('dossiers-empty-state');
  const pageTabAgenda = document.getElementById('page-tab-agenda');
  const pageTabLedgers = document.getElementById('page-tab-ledgers');
  const pageAgenda = document.getElementById('page-agenda');
  const pageLedgers = document.getElementById('page-ledgers');
  const commitmentsListEl = document.getElementById('commitments-list');
  const commitmentsEmptyStateEl = document.getElementById('commitments-empty-state');
  const accomplishmentsListEl = document.getElementById('accomplishments-list');
  const accomplishmentsEmptyStateEl = document.getElementById('accomplishments-empty-state');
  const formAddCommitment = document.getElementById('form-add-commitment');
  const commitmentDescriptionInput = document.getElementById('commitment-description');
  const commitmentDueAtInput = document.getElementById('commitment-due-at');
  const btnAddCommitment = document.getElementById('btn-add-commitment');
  const formAddAccomplishment = document.getElementById('form-add-accomplishment');
  const accomplishmentDescriptionInput = document.getElementById('accomplishment-description');
  const accomplishmentGoalSelect = document.getElementById('accomplishment-goal');
  const btnAddAccomplishment = document.getElementById('btn-add-accomplishment');
  const pageTabChat = document.getElementById('page-tab-chat');
  const pageChat = document.getElementById('page-chat');
  const pageTabDossiers = document.getElementById('page-tab-dossiers');
  const pageDossiers = document.getElementById('page-dossiers');
  const pageTabGraph = document.getElementById('page-tab-graph');
  const pageGraph = document.getElementById('page-graph');
  const graphCanvas = document.getElementById('graph-canvas');
  const graphReviewList = document.getElementById('graph-review-list');
  const graphReviewCount = document.getElementById('graph-review-count');
  const btnRefreshGraph = document.getElementById('btn-refresh-graph');
  const graphDetails = document.getElementById('graph-details');
  const chatMessagesEl = document.getElementById('chat-messages');
  const chatEmptyStateEl = document.getElementById('chat-empty-state');
  const formChat = document.getElementById('form-chat');
  const chatInput = document.getElementById('chat-input');
  const btnChatSend = document.getElementById('btn-chat-send');
  const chatSendText = document.getElementById('chat-send-text');
  const chatSendSpinner = document.getElementById('chat-send-spinner');
  const chatSessionEyebrow = document.getElementById('chat-session-eyebrow');
  const btnChatReset = document.getElementById('btn-chat-reset');
  const agentBubbleContainerEl = document.getElementById('agent-bubble-container');
  const agentOrbEl = document.getElementById('agent-orb');
  const agentBubbleTitleEl = document.getElementById('agent-bubble-title');
  const agentBubbleTextEl = document.getElementById('agent-bubble-text');
  const agentBubblePlayEl = document.getElementById('agent-bubble-play');
  const agentBubblePulseCardEl = document.getElementById('agent-bubble-pulse-card');
  const agentBubbleChecklistEl = document.getElementById('agent-bubble-checklist');
  const agentBubbleDismissEl = document.getElementById('agent-bubble-dismiss');
  const agentBubbleAudioEl = document.getElementById('agent-bubble-audio');
  const checklistChipEl = document.getElementById('checklist-chip');
  const checklistChipTextEl = document.getElementById('checklist-chip-text');
  const checklistOverlayEl = document.getElementById('checklist-overlay');
  const btnChecklistClose = document.getElementById('btn-checklist-close');
  const checklistProgressFillEl = document.getElementById('checklist-progress-fill');
  const checklistProgressLabelEl = document.getElementById('checklist-progress-label');
  const checklistPendingListEl = document.getElementById('checklist-pending-list');
  const checklistEmptyStateEl = document.getElementById('checklist-empty-state');
  const checklistResolvedDividerEl = document.getElementById('checklist-resolved-divider');
  const checklistResolvedListEl = document.getElementById('checklist-resolved-list');
  const meetingsChipEl = document.getElementById('meetings-chip');
  const meetingsChipTextEl = document.getElementById('meetings-chip-text');
  const meetingsOverlayEl = document.getElementById('meetings-overlay');
  const btnMeetingsClose = document.getElementById('btn-meetings-close');
  const meetingsListEl = document.getElementById('meetings-list');
  const meetingsEmptyStateEl = document.getElementById('meetings-empty-state');
  const btnMentorAdvice = document.getElementById('btn-mentor-advice');
  const mentorAdvisorOverlayEl = document.getElementById('mentor-advisor-overlay');
  const btnMentorAdvisorClose = document.getElementById('btn-mentor-advisor-close');
  const btnMentorAdvisorRefresh = document.getElementById('btn-mentor-advisor-refresh');
  const btnMentorAdvisorRetry = document.getElementById('btn-mentor-advisor-retry');
  const btnMentorCopy = document.getElementById('btn-mentor-copy');
  const mentorAdvisorLoadingEl = document.getElementById('mentor-advisor-loading');
  const mentorAdvisorErrorEl = document.getElementById('mentor-advisor-error');
  const mentorAdvisorErrorTextEl = document.getElementById('mentor-advisor-error-text');
  const mentorAdvisorContentEl = document.getElementById('mentor-advisor-content');
  const mentorStreamStatusEl = document.getElementById('mentor-stream-status');
  const mentorLoadingStageTitle = document.getElementById('mentor-loading-stage-title');
  const mentorLoadingStageDesc = document.getElementById('mentor-loading-stage-desc');
  const mentorAdviceShortTermEl = document.getElementById('mentor-advice-short-term');
  const mentorAdviceLongTermEl = document.getElementById('mentor-advice-long-term');
  const mentorAdviceRationaleEl = document.getElementById('mentor-advice-rationale');

  // Initialize App — shows the login overlay first; the dashboard itself
  // only starts fetching real data once a real identity is resolved
  // (either a fresh login or a restored sessionStorage session).
  function init() {
    state.agendaItems = [...mockInitialItems];
    state.staleItems = [...mockStaleItems];

    setupEventListeners();
    setupVisibilitySelector();
    setupCharCounter();
    setupPageTabs();
    setupLoginForm();
    setupPreferences();
    setupChecklist();
    setupMeetings();
    setupMentorAdvisor();
    setupLedgerForms();
    setupChat();
    checkBackendStatus();
    setInterval(checkBackendStatus, 15000);

    const setupToken = new URL(window.location.href).searchParams.get('setup_token');
    if (setupToken) {
      redeemSetupToken(setupToken);
      return;
    }

    const restored = restoreSession();
    if (restored) {
      enterApp(restored);
    }

    // Landing spot for app/triggers/google_oauth_router.py's callback
    // redirect (/ui?google_connected=1 or ?google_error=...) — the OAuth
    // round trip left this tab and came back, so sessionStorage-restored
    // state above is what makes the toast meaningful (which account this
    // was for). Stripped from the URL immediately so a refresh doesn't
    // re-show it.
    const params = new URL(window.location.href).searchParams;
    if (params.get('google_connected')) {
      showToast('Google account connected.', 'success');
      window.history.replaceState({}, '', window.location.pathname);
    } else if (params.get('google_error')) {
      showToast(`Google connect failed: ${params.get('google_error')}`, 'error');
      window.history.replaceState({}, '', window.location.pathname);
    }
  }

  // sessionStorage (not localStorage) — clears when the tab closes,
  // matching this dashboard's existing "demo, not production auth"
  // posture (see login's own docstring on why email+secret rather than
  // a real session-cookie/JWT system) while still surviving a page
  // refresh, which a real login flow needs to feel usable at all.
  function persistSession() {
    sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({
      actingUserId: state.actingUserId,
      userSecret: state.userSecret,
      displayName: state.displayName,
      loginEmail: state.loginEmail,
      managedReports: state.managedReports,
      reportUserId: state.reportUserId,
      actingRole: state.actingRole
    }));
  }

  function restoreSession() {
    try {
      const raw = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (!raw) return false;
      const saved = JSON.parse(raw);
      if (!saved.actingUserId || !saved.userSecret || !saved.reportUserId) return false;
      Object.assign(state, saved);
      return true;
    } catch {
      return false;
    }
  }

  function logout() {
    sessionStorage.removeItem(SESSION_STORAGE_KEY);
    if (state.pollInterval) clearInterval(state.pollInterval);
    window.location.reload();
  }

  // Login (POST /webhooks/login) — real auth, see the route's own
  // docstring: email + the agenda_client_secret, not email alone.
  function setupLoginForm() {
    formLogin.addEventListener('submit', handleLogin);
    btnLogout.addEventListener('click', logout);
    pairSwitcher.addEventListener('change', () => {
      selectAgendaReport(pairSwitcher.value);
    });

    linkRequestSetup.addEventListener('click', (e) => {
      e.preventDefault();
      loginView.style.display = 'none';
      requestSetupView.style.display = 'block';
    });
    linkBackToLogin.addEventListener('click', (e) => {
      e.preventDefault();
      requestSetupView.style.display = 'none';
      loginView.style.display = 'block';
    });
    formRequestSetup.addEventListener('submit', handleRequestSetup);
    btnCopySecret.addEventListener('click', () => {
      navigator.clipboard.writeText(setupSecretBox.textContent).catch(() => {});
    });
    btnContinueToLogin.addEventListener('click', () => {
      setupCompleteView.style.display = 'none';
      loginView.style.display = 'block';
    });
  }

  // GET a real setup link by email — POST /webhooks/request-setup-link.
  // Always shows the same success message regardless of whether the
  // email is actually registered (the backend is deliberately generic
  // for the same reason — see that route's own docstring).
  async function handleRequestSetup(e) {
    e.preventDefault();
    const email = setupRequestEmailInput.value.trim();
    requestSetupBtnText.textContent = 'Sending…';
    requestSetupSpinner.style.display = 'inline-block';
    btnRequestSetup.disabled = true;

    try {
      await fetch('/webhooks/request-setup-link', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Agenda-Token': state.sharedToken },
        body: JSON.stringify({ email })
      });
      requestSetupSuccess.style.display = 'block';
      formRequestSetup.style.display = 'none';
    } catch {
      // Backend unreachable — leave the form up rather than falsely
      // claiming success.
    } finally {
      requestSetupBtnText.textContent = 'Send setup link';
      requestSetupSpinner.style.display = 'none';
      btnRequestSetup.disabled = false;
    }
  }

  // Redeems a ?setup_token= from the emailed link — POST
  // /webhooks/complete-setup. Called automatically on page load when
  // the URL carries one (see init()).
  async function redeemSetupToken(token) {
    try {
      const response = await fetch('/webhooks/complete-setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Agenda-Token': state.sharedToken },
        body: JSON.stringify({ token })
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        loginErrorEl.textContent = body.reason || 'This setup link is invalid or has expired.';
        loginErrorEl.style.display = 'block';
        return;
      }
      setupSecretBox.textContent = body.secret;
      loginEmailInput.value = body.email || '';
      loginView.style.display = 'none';
      setupCompleteView.style.display = 'block';
    } catch (err) {
      loginErrorEl.textContent = `Could not reach the backend — ${err.message}`;
      loginErrorEl.style.display = 'block';
    } finally {
      // Strip setup_token from the URL either way — it's single-use and
      // must never be reusable via back/refresh.
      const url = new URL(window.location.href);
      url.searchParams.delete('setup_token');
      window.history.replaceState({}, '', url.toString());
    }
  }

  async function handleLogin(e) {
    e.preventDefault();
    const email = loginEmailInput.value.trim();
    const secret = loginSecretInput.value;
    loginErrorEl.style.display = 'none';
    loginBtnText.textContent = 'Signing in…';
    loginSpinner.style.display = 'inline-block';
    btnLogin.disabled = true;

    try {
      const response = await fetch('/webhooks/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken
        },
        body: JSON.stringify({ email, secret })
      });
      const body = await response.json().catch(() => ({}));

      if (!response.ok) {
        loginErrorEl.textContent = body.reason || 'Invalid email or secret.';
        loginErrorEl.style.display = 'block';
        return;
      }

      // Every user has exactly one dashboard — their own. managed_reports
      // (if any) only populates the Rolling Agenda tab's own report
      // picker; it never decides which dashboard gets entered.
      state.actingUserId = body.user_id;
      state.displayName = body.display_name;
      state.loginEmail = email;
      state.userSecret = secret;
      state.managedReports = body.managed_reports || [];
      state.reportUserId = body.user_id;
      state.actingRole = 'report';
      persistSession();
      enterApp();
    } catch (err) {
      loginErrorEl.textContent = `Could not reach the backend — ${err.message}`;
      loginErrorEl.style.display = 'block';
    } finally {
      loginBtnText.textContent = 'Sign in';
      loginSpinner.style.display = 'none';
      btnLogin.disabled = false;
    }
  }

  // Switches which report's agenda/notes the Rolling Agenda tab shows —
  // scoped to that tab only (Dossiers, the live agent bubble, etc. all
  // stay on state.actingUserId regardless of this). NOT goals/OKRs
  // anymore (REAL CHANGE, requested: "a manager can't see okrs and goals
  // of their reports and vice versa") — fetchGoals is always self-scoped
  // now, so it doesn't need refetching when this switches. Only
  // reachable at all when state.managedReports is non-empty; state.
  // reportUserId is set to the first managed report by
  // renderAgendaReportPicker, never left on "self" — see that
  // function's own comment on why there's no "Myself" option here.
  function selectAgendaReport(reportUserId) {
    state.reportUserId = reportUserId;
    state.actingRole = reportUserId === state.actingUserId ? 'report' : 'manager';
    persistSession();
    fetchPayload();
    fetchNotes();
  }

  // Runs once a real identity is resolved (fresh login or a restored
  // session) — starts fetching real data and reveals the dashboard.
  function enterApp() {
    loginOverlay.style.display = 'none';
    appContainer.style.display = '';
    renderAccountIndicator();

    fetchPayload();
    fetchGoals();
    fetchNotes();
    fetchDossiers();
    fetchGraph();
    fetchChecklist();
    initLiveNotifications();

    // These timers are now a FALLBACK safety net, not the primary update
    // path — initLiveNotifications' agenda_changed/goals_changed/
    // notes_changed/checklist_changed SSE events below fire an immediate
    // refetch the moment the DB actually changes, same "poll server-side,
    // push over SSE" relay live_notifications_router.py already used for
    // the pulse/dossier bubble. Widened from the old 4s/20s (which WAS
    // the only update path, hence the staleness complaint) since a
    // healthy SSE connection makes these redundant most of the time;
    // still here in case that connection drops (EventSource.onerror
    // below) or misses something.
    if (state.pollInterval) clearInterval(state.pollInterval);
    state.pollInterval = setInterval(fetchPayload, 8000);
    setInterval(fetchGoals, 30000);
    setInterval(fetchNotes, 30000);
    setInterval(fetchDossiers, 30000);
    setInterval(fetchChecklist, 30000);
    // Gated on the graph tab actually being open — the render below
    // tears down and re-lays-out the whole force graph from scratch, so
    // polling it in the background (even to a no-op tab) meant the graph
    // visibly re-animated/jumped every 20s for anyone with the tab open,
    // and did needless work for anyone who didn't.
    setInterval(() => { if (state.currentPage === 'graph') fetchGraph(); }, 20000);
  }

  // ── Live Agent Bubble ────────────────────────────────────────────
  // Pushes a "heads up" popup the moment a pulse, dossier, or Friday
  // review lands for this owner (server polls the DB and relays over
  // SSE — see app/triggers/live_notifications_router.py's own docstring
  // for why it's poll-relayed rather than a true cross-process event
  // bus). Always state.actingUserId (self) — every user has exactly one
  // dashboard; a manager viewing a report's Rolling Agenda tab never
  // gets bubbles for that report's own private rituals.
  function initLiveNotifications() {
    if (state.notificationsSource) {
      state.notificationsSource.close();
    }
    if (!state.actingUserId || !state.sharedToken || !state.userSecret) return;

    const url = `/webhooks/notifications?owner_user_id=${encodeURIComponent(state.actingUserId)}`
      + `&token=${encodeURIComponent(state.sharedToken)}`
      + `&secret=${encodeURIComponent(state.userSecret)}`;
    const source = new EventSource(url);
    state.notificationsSource = source;

    source.addEventListener('dossier_ready', (evt) => {
      const data = JSON.parse(evt.data);
      showAgentBubble({
        kind: 'dossier',
        deliveryId: data.id,
        title: data.who ? `Prepping you for ${data.who}` : 'Your dossier is ready',
        text: data.why_now || 'Take a look before the meeting starts.',
      });
      fetchDossiers();
    });

    source.addEventListener('friday_review_ready', (evt) => {
      const data = JSON.parse(evt.data);
      const itemText = data.proposed_item_count > 0
        ? `${data.proposed_item_count} item(s) ready to log to your ledger.`
        : 'Take a look when you have a moment.';
      showAgentBubble({
        kind: 'friday_review',
        deliveryId: data.id,
        title: `Your Friday reflection — week of ${data.week_start_date}`,
        text: itemText,
      });
    });

    source.addEventListener('pulse_ready', (evt) => {
      const data = JSON.parse(evt.data);
      const countWord = data.item_count === 1 ? '1 item' : `${data.item_count} items`;
      showAgentBubble({
        kind: 'pulse',
        deliveryId: data.id,
        title: 'Your morning pulse is ready',
        text: data.item_count > 0 ? `${countWord} in your focus today.` : 'A quiet morning — nothing cleared the bar.',
        card: data.card,
      });
      // A fresh pulse can introduce new actionable items (or resolve old
      // ones) — refresh the checklist panel/chip so they show up without
      // waiting for the next 20s poll.
      fetchChecklist();
    });

    // Real-time dashboard refresh signals — the server saw a real DB
    // change (new/updated agenda item, goal progress, note, or checklist
    // completion) and is telling the browser to go refetch NOW instead of
    // waiting out its own fallback timer above. Payload is intentionally
    // empty ({}) — these are "go refetch" signals, not the data itself,
    // so the existing fetch*() functions stay the single source of truth
    // for how each payload is assembled (visibility rules, scoping, etc.)
    // rather than duplicating that logic into the SSE relay too.
    source.addEventListener('agenda_changed', () => { fetchPayload(); });
    source.addEventListener('goals_changed', () => { fetchGoals(); });
    source.addEventListener('notes_changed', () => { fetchNotes(); });
    source.addEventListener('checklist_changed', () => { fetchChecklist(); });
    // Own listener (not lumped with the four above): those four just
    // refetch a payload the picker already assumes exists; this one can
    // also change WHETHER the picker shows at all (a first report just
    // got paired, or a manager's last report just ended) — real state
    // change to state.managedReports, not just a "go re-render the same
    // thing" signal.
    source.addEventListener('managed_reports_changed', () => { fetchManagedReports(); });

    // EventSource retries on its own after a transport error — nothing
    // to do here beyond not crashing the page (matches fetchDossiers'
    // own "offline/standalone mode" fallback posture elsewhere).
    source.onerror = () => {};
  }

  function showAgentBubble({ kind, deliveryId, title, text, card }) {
    if (!agentBubbleContainerEl) return;
    agentBubbleTitleEl.textContent = title;
    agentBubblePlayEl.textContent = '▶ Play';
    agentBubblePlayEl.disabled = false;
    agentBubblePlayEl.dataset.kind = kind;
    agentBubblePlayEl.dataset.deliveryId = deliveryId;
    if (agentBubbleChecklistEl) {
      // Only the pulse bubble opens the checklist — the notification stays
      // up (no auto-hide, see below) and clicking this is how it "opens
      // the todo that's always reachable throughout the day."
      agentBubbleChecklistEl.style.display = kind === 'pulse' ? '' : 'none';
    }

    // Pulse gets the full card (Focus items with why-now/action, meeting
    // load, owed) inline in the bubble instead of a bare count — same SSE
    // push, richer payload (app/triggers/live_notifications_router.py's
    // pulse_ready event now carries card_to_dict's shape). Falls back to
    // the plain summary line for a delivery written before that column
    // existed (card is {}), and for every other bubble kind, which never
    // sends one at all.
    const hasPulseCard = kind === 'pulse' && card && (card.focus || []).length + (card.day || []).length + (card.owed || []).length > 0;
    if (hasPulseCard && agentBubblePulseCardEl) {
      agentBubbleTextEl.style.display = 'none';
      agentBubblePulseCardEl.innerHTML = renderPulseBubbleCard(card);
      agentBubblePulseCardEl.style.display = '';
      agentBubbleContainerEl.classList.add('pulse-expanded');
    } else {
      agentBubbleTextEl.textContent = text;
      agentBubbleTextEl.style.display = '';
      if (agentBubblePulseCardEl) {
        agentBubblePulseCardEl.style.display = 'none';
        agentBubblePulseCardEl.innerHTML = '';
      }
      agentBubbleContainerEl.classList.remove('pulse-expanded');
    }

    agentOrbEl.classList.remove('speaking');
    agentBubbleContainerEl.classList.add('visible');
    // Stays up until the user dismisses it or plays the audio — no
    // auto-hide timer. A silent auto-dismiss meant a notification could
    // vanish before anyone ever saw it; the explicit Dismiss button is
    // the only way this closes on its own now.
  }

  function _pulseFocusItemHtml(item) {
    const marker = item.novelty === true ? ' 🆕' : (item.novelty === false ? ' 🔁' : '');
    const safeTitle = escapeHtml(item.title || '(untitled)');
    const link = safeSourceLink(item.url);
    const heading = link
      ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${safeTitle}</a>`
      : safeTitle;
    return `
      <div class="pulse-bubble-focus-item">
        <div class="pulse-bubble-focus-title">${heading}${marker}</div>
        ${item.detail ? `<div class="pulse-bubble-focus-detail">${escapeHtml(item.detail)}</div>` : ''}
        <div class="pulse-bubble-focus-why">${escapeHtml(item.why_now || '')}</div>
        <div class="pulse-bubble-focus-action">${escapeHtml(item.action || '')}</div>
      </div>`;
  }

  function renderPulseBubbleCard(card) {
    const focus = card.focus || [];
    const day = card.day || [];
    const owed = card.owed || [];
    const parts = [];

    if (card.degradation_line) {
      parts.push(`<div class="pulse-bubble-degradation">⚠️ ${escapeHtml(card.degradation_line)}</div>`);
    }

    parts.push(`<div class="pulse-bubble-section-label">🎯 Focus${card.since_note ? ` <span class="pulse-bubble-since">(${escapeHtml(card.since_note)})</span>` : ''}</div>`);
    if (focus.length === 0) {
      parts.push('<div class="pulse-bubble-empty">nothing cleared the bar today</div>');
    } else {
      parts.push(focus.map(_pulseFocusItemHtml).join(''));
    }
    if (card.total_count > card.shown_count) {
      parts.push(`<div class="pulse-bubble-meta">${card.shown_count} of ${card.total_count} — see full shortlist in Slack</div>`);
    }
    const alsoLabels = { work_item: 'work item', message: 'message' };
    const alsoParts = [];
    for (const t of ['message', 'work_item']) {
      const count = (card.also_happening_counts || {})[t] || 0;
      if (count) alsoParts.push(`${count} ${alsoLabels[t]}${count !== 1 ? 's' : ''}`);
    }
    if (alsoParts.length) {
      parts.push(`<div class="pulse-bubble-meta">💬 Also happening: ${alsoParts.join(', ')}</div>`);
    }

    parts.push('<div class="pulse-bubble-section-label pulse-bubble-section-label-spaced">🗓️ Today\'s meeting load</div>');
    if (day.length === 0) {
      parts.push('<div class="pulse-bubble-empty">clear</div>');
    } else {
      parts.push(day.map((e) => {
        const time = e.starts_at ? new Date(e.starts_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
        const safeTitle = escapeHtml(e.title || '');
        const link = safeSourceLink(e.url);
        const titleHtml = link
          ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${safeTitle}</a>`
          : safeTitle;
        return `<div class="pulse-bubble-day-row">${escapeHtml(time)} ${titleHtml}${e.has_dossier ? ' 📄' : ''}</div>`;
      }).join(''));
    }

    parts.push('<div class="pulse-bubble-section-label pulse-bubble-section-label-spaced">🤝 Owed</div>');
    if (owed.length === 0) {
      parts.push('<div class="pulse-bubble-empty">nothing owed</div>');
    } else {
      parts.push(owed.map((o) => `<div class="pulse-bubble-day-row">${escapeHtml(o.description || '')} — ${escapeHtml(o.promised_to || '')}${o.overdue ? ' 🔴' : ''}</div>`).join(''));
    }

    if (card.suggested_focus) {
      parts.push(`<div class="pulse-bubble-suggested">🔥 ${escapeHtml(card.suggested_focus)}</div>`);
    }

    return parts.join('');
  }

  function hideAgentBubble() {
    if (!agentBubbleContainerEl) return;
    agentBubbleContainerEl.classList.remove('visible');
    agentBubbleContainerEl.classList.remove('pulse-expanded');
    agentBubbleAudioEl.pause();
    agentOrbEl.classList.remove('speaking');
  }

  // Shared by the live agent bubble's own Play button and every per-card
  // Play button on the Dossiers tab (dossier-play-btn) — one fetch+play
  // path, one <audio> element, so two clicks in a row correctly stop
  // the first clip rather than overlapping it.
  async function fetchAndPlayAudio(kind, deliveryId, buttonEl) {
    if (!kind || !deliveryId) return;
    const endpoint = kind === 'friday_review'
      ? 'friday-review-audio'
      : kind === 'pulse' ? 'pulse-audio' : 'dossier-audio';
    const url = `/webhooks/${endpoint}?owner_user_id=${encodeURIComponent(state.actingUserId)}`
      + `&delivery_id=${encodeURIComponent(deliveryId)}`
      + `&token=${encodeURIComponent(state.sharedToken)}`
      + `&secret=${encodeURIComponent(state.userSecret)}`;

    const originalText = buttonEl.textContent;
    buttonEl.disabled = true;
    buttonEl.textContent = 'Loading…';
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error('audio unavailable');
      const blob = await response.blob();
      agentBubbleAudioEl.pause();
      agentBubbleAudioEl.src = URL.createObjectURL(blob);
      if (agentOrbEl) agentOrbEl.classList.add('speaking');
      buttonEl.textContent = originalText;
      buttonEl.disabled = false;
      await agentBubbleAudioEl.play();
      agentBubbleAudioEl.onended = () => { if (agentOrbEl) agentOrbEl.classList.remove('speaking'); };
    } catch {
      buttonEl.textContent = 'Unavailable';
      buttonEl.disabled = false;
      if (agentOrbEl) agentOrbEl.classList.remove('speaking');
    }
  }

  function playAgentBubbleAudio() {
    return fetchAndPlayAudio(
      agentBubblePlayEl.dataset.kind, agentBubblePlayEl.dataset.deliveryId, agentBubblePlayEl
    );
  }

  // ── Morning-Pulse Checklist ──────────────────────────────────────
  // A persistent to-do view over app/salience/checklist.py's own
  // build_checklist: pending is exactly today's non-event pulse
  // shortlist (Focus + "also happening" — see app/pipeline/pulse.py's
  // ranking_pool), resolved is either a manual check-off or an
  // auto-detected resolve (a merged PR, a closed ticket). Reachable via
  // the header chip any time, not just right after a pulse lands —
  // that's the whole point of pinning it, same as Slack's own "N tasks
  // left" sidebar entry.
  function setupChecklist() {
    if (checklistChipEl) checklistChipEl.addEventListener('click', openChecklistPanel);
    if (btnChecklistClose) btnChecklistClose.addEventListener('click', closeChecklistPanel);
    if (checklistOverlayEl) {
      checklistOverlayEl.addEventListener('click', (e) => {
        if (e.target === checklistOverlayEl) closeChecklistPanel();
      });
    }
    // Rows are re-rendered wholesale on every fetch (renderChecklist), so
    // checkbox clicks are handled via delegation on the static list
    // container rather than per-row listeners that would need rebinding
    // after every innerHTML replace.
    if (checklistPendingListEl) {
      checklistPendingListEl.addEventListener('click', (e) => {
        const checkbox = e.target.closest('.checklist-checkbox');
        if (!checkbox) return;
        const row = checkbox.closest('.checklist-item');
        if (!row) return;
        completeChecklistItem(row.dataset.itemType, row.dataset.itemId, checkbox);
      });
    }
  }

  function openChecklistPanel() {
    if (!checklistOverlayEl) return;
    checklistOverlayEl.style.display = 'flex';
    fetchChecklist();
  }

  function closeChecklistPanel() {
    if (checklistOverlayEl) checklistOverlayEl.style.display = 'none';
  }

  async function fetchChecklist() {
    if (!state.actingUserId || !state.sharedToken || !state.userSecret) return;
    try {
      const response = await fetch(
        `/webhooks/checklist?owner_user_id=${encodeURIComponent(state.actingUserId)}`,
        {
          headers: {
            'X-Agenda-Token': state.sharedToken,
            'X-Acting-User-Secret': state.userSecret
          }
        }
      );
      if (response.ok) {
        renderChecklist(await response.json());
      }
    } catch {
      // Offline/standalone mode — leave whatever's already rendered showing,
      // same fallback posture as fetchGoals/fetchDossiers above.
    }
  }

  function renderChecklist(data) {
    state.checklistData = data;
    const pending = data.pending || [];
    const resolved = data.resolved || [];
    const stats = data.stats || { done: 0, total: 0 };

    if (checklistChipEl) {
      const left = pending.length;
      checklistChipTextEl.textContent = left === 0
        ? (stats.total > 0 ? 'All done' : '0 tasks left')
        : `${left} task${left === 1 ? '' : 's'} left`;
      checklistChipEl.classList.toggle('checklist-chip-clear', left === 0 && stats.total > 0);
      checklistChipEl.style.display = stats.total > 0 ? '' : 'none';
    }

    if (checklistProgressFillEl) {
      const pct = stats.total > 0 ? Math.round((stats.done / stats.total) * 100) : 0;
      checklistProgressFillEl.style.width = `${pct}%`;
    }
    if (checklistProgressLabelEl) {
      checklistProgressLabelEl.textContent = `${stats.done} / ${stats.total}`;
    }

    if (checklistPendingListEl) {
      checklistPendingListEl.innerHTML = pending.map(renderChecklistPendingRow).join('');
    }
    if (checklistEmptyStateEl) {
      checklistEmptyStateEl.style.display = pending.length === 0 ? 'flex' : 'none';
    }

    if (checklistResolvedDividerEl) {
      checklistResolvedDividerEl.style.display = resolved.length > 0 ? '' : 'none';
    }
    if (checklistResolvedListEl) {
      checklistResolvedListEl.innerHTML = resolved.map(renderChecklistResolvedRow).join('');
    }

    // Today's meetings ride the same /webhooks/checklist response (its
    // own "day" field) rather than a separate fetch — same data
    // build_checklist already computed for pending/resolved above, so
    // this doesn't cost a second round trip against a remote Postgres.
    renderMeetings(data.day || []);
  }

  function _checklistItemTitleHtml(item) {
    const safeTitle = escapeHtml(item.title || '(untitled)');
    const link = safeSourceLink(item.url);
    return link
      ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${safeTitle}</a>`
      : safeTitle;
  }

  function renderChecklistPendingRow(item) {
    return `
      <div class="checklist-item" data-item-type="${escapeHtml(item.item_type)}" data-item-id="${escapeHtml(item.item_id)}">
        <button type="button" class="checklist-checkbox" aria-label="Mark done"></button>
        <div class="checklist-item-body">
          <div class="checklist-item-title">${_checklistItemTitleHtml(item)}</div>
          ${item.detail ? `<div class="checklist-item-detail">${escapeHtml(item.detail)}</div>` : ''}
        </div>
      </div>`;
  }

  function renderChecklistResolvedRow(item) {
    const sourceLabel = item.source === 'auto' ? 'Auto-detected' : 'Done';
    const isPendingAi = item.ai_response === 'Saving…';
    return `
      <div class="checklist-item just-resolved">
        <span class="checklist-item-resolved-icon">✓</span>
        <div class="checklist-item-body">
          <div class="checklist-item-title">${_checklistItemTitleHtml(item)}</div>
          ${item.ai_response ? `<div class="checklist-ai-response${isPendingAi ? ' pending' : ''}">${escapeHtml(item.ai_response)}</div>` : ''}
        </div>
        <span class="checklist-source-tag">${sourceLabel}</span>
      </div>`;
  }

  // ── Today's Meetings ─────────────────────────────────────────────
  // Same "always reachable via a header chip" pattern as the checklist
  // above — data comes along with every fetchChecklist() call (see
  // renderChecklist's own comment), this section just owns its own
  // chip/panel and rendering.
  function setupMeetings() {
    if (meetingsChipEl) meetingsChipEl.addEventListener('click', openMeetingsPanel);
    if (btnMeetingsClose) btnMeetingsClose.addEventListener('click', closeMeetingsPanel);
    if (meetingsOverlayEl) {
      meetingsOverlayEl.addEventListener('click', (e) => {
        if (e.target === meetingsOverlayEl) closeMeetingsPanel();
      });
    }
    if (meetingsListEl) {
      meetingsListEl.addEventListener('click', (e) => {
        const viewBtn = e.target.closest('.meeting-dossier-view');
        if (!viewBtn) return;
        e.preventDefault();
        viewDossier(viewBtn.dataset.dossierId);
      });
    }
  }

  function openMeetingsPanel() {
    if (!meetingsOverlayEl) return;
    meetingsOverlayEl.style.display = 'flex';
    fetchChecklist();
  }

  function closeMeetingsPanel() {
    if (meetingsOverlayEl) meetingsOverlayEl.style.display = 'none';
  }

  function renderMeetings(day) {
    if (meetingsChipEl) {
      meetingsChipTextEl.textContent = `${day.length} meeting${day.length === 1 ? '' : 's'} today`;
      meetingsChipEl.style.display = day.length > 0 ? '' : 'none';
    }
    if (meetingsListEl) {
      meetingsListEl.innerHTML = day.map(renderMeetingRow).join('');
    }
    if (meetingsEmptyStateEl) {
      meetingsEmptyStateEl.style.display = day.length === 0 ? 'flex' : 'none';
    }
  }

  function renderMeetingRow(event) {
    const time = event.starts_at
      ? new Date(event.starts_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : '';
    const safeTitle = escapeHtml(event.title || '(untitled)');
    const link = safeSourceLink(event.url);
    const titleHtml = link
      ? `<a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${safeTitle}</a>`
      : safeTitle;
    const dossierHtml = event.dossier_id
      ? `<div class="meeting-dossier-ready">
           <span>📄 Dossier ready</span>
           <button type="button" class="meeting-dossier-view" data-dossier-id="${escapeHtml(event.dossier_id)}">View</button>
         </div>`
      : '';
    return `
      <div class="checklist-item">
        <div class="checklist-item-body">
          <div class="checklist-item-title">${escapeHtml(time)} — ${titleHtml}</div>
          ${dossierHtml}
        </div>
      </div>`;
  }

  // ── Mentor Advisor ───────────────────────────────────────────────
  // On-demand "what should I be working on" popup next to the
  // Accomplishments ledger (requested: "a popup agent ... like a mentor
  // that constantly advises the user ... similar to what lands in the
  // friday reflection"). A real LLM call each open, same context Friday
  // review itself synthesizes from (goals/OKR progress, career goal,
  // skill distribution, stuck agenda items) — not cached, so it's always
  // grounded in whatever's current when the user actually asks.
  let activeMentorStreamingTimeouts = [];
  let mentorStepInterval = null;

  function clearMentorStreaming() {
    activeMentorStreamingTimeouts.forEach((t) => clearTimeout(t));
    activeMentorStreamingTimeouts = [];
    if (mentorStepInterval) {
      clearInterval(mentorStepInterval);
      mentorStepInterval = null;
    }
  }

  function setupMentorAdvisor() {
    if (btnMentorAdvice) btnMentorAdvice.addEventListener('click', openMentorAdvisorPanel);
    if (btnMentorAdvisorClose) btnMentorAdvisorClose.addEventListener('click', closeMentorAdvisorPanel);
    if (btnMentorAdvisorRefresh) btnMentorAdvisorRefresh.addEventListener('click', fetchMentorAdvice);
    if (btnMentorAdvisorRetry) btnMentorAdvisorRetry.addEventListener('click', fetchMentorAdvice);
    if (btnMentorCopy) btnMentorCopy.addEventListener('click', copyMentorAdvice);
    if (mentorAdvisorOverlayEl) {
      mentorAdvisorOverlayEl.addEventListener('click', (e) => {
        if (e.target === mentorAdvisorOverlayEl) closeMentorAdvisorPanel();
      });
    }
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && mentorAdvisorOverlayEl && mentorAdvisorOverlayEl.style.display === 'flex') {
        closeMentorAdvisorPanel();
      }
    });
  }

  function openMentorAdvisorPanel() {
    if (!mentorAdvisorOverlayEl) return;
    mentorAdvisorOverlayEl.style.display = 'flex';
    fetchMentorAdvice();
  }

  function closeMentorAdvisorPanel() {
    clearMentorStreaming();
    if (mentorAdvisorOverlayEl) mentorAdvisorOverlayEl.style.display = 'none';
  }

  async function fetchMentorAdvice() {
    if (!state.actingUserId || !state.sharedToken || !state.userSecret) return;

    clearMentorStreaming();

    if (mentorAdvisorLoadingEl) mentorAdvisorLoadingEl.style.display = 'flex';
    if (mentorAdvisorErrorEl) mentorAdvisorErrorEl.style.display = 'none';
    if (mentorAdvisorContentEl) mentorAdvisorContentEl.style.display = 'none';

    if (mentorLoadingStageTitle) mentorLoadingStageTitle.textContent = 'Analyzing your workspace…';
    if (mentorLoadingStageDesc) mentorLoadingStageDesc.textContent = 'Reading OKRs, 1:1 notes, commitments, and stuck items.';

    const steps = ['mstep-1', 'mstep-2', 'mstep-3'];
    steps.forEach((s, idx) => {
      const el = document.getElementById(s);
      if (el) {
        el.classList.toggle('active', idx === 0);
        el.classList.remove('done');
      }
    });

    let currentStep = 0;
    mentorStepInterval = setInterval(() => {
      if (currentStep < 2) {
        const prevEl = document.getElementById(steps[currentStep]);
        if (prevEl) {
          prevEl.classList.remove('active');
          prevEl.classList.add('done');
        }
        currentStep++;
        const nextEl = document.getElementById(steps[currentStep]);
        if (nextEl) nextEl.classList.add('active');
        if (currentStep === 1 && mentorLoadingStageTitle) {
          mentorLoadingStageTitle.textContent = 'Evaluating trajectory & goals…';
        } else if (currentStep === 2 && mentorLoadingStageTitle) {
          mentorLoadingStageTitle.textContent = 'Synthesizing actionable advice…';
        }
      }
    }, 850);

    try {
      const response = await fetch(
        `/webhooks/mentor-advice?owner_user_id=${encodeURIComponent(state.actingUserId)}`,
        {
          headers: {
            'X-Agenda-Token': state.sharedToken,
            'X-Acting-User-Secret': state.userSecret
          }
        }
      );
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.reason || 'request failed');
      }
      if (mentorStepInterval) clearInterval(mentorStepInterval);
      state.lastMentorAdvice = data;
      renderMentorAdviceProgressive(data);
    } catch (err) {
      if (mentorStepInterval) clearInterval(mentorStepInterval);
      if (mentorAdvisorErrorTextEl) {
        mentorAdvisorErrorTextEl.textContent = err.message || 'Try again in a moment.';
      }
      if (mentorAdvisorErrorEl) mentorAdvisorErrorEl.style.display = 'flex';
    } finally {
      if (mentorAdvisorLoadingEl) mentorAdvisorLoadingEl.style.display = 'none';
    }
  }

  function streamTextIntoElement(targetEl, text, speedMs = 15) {
    return new Promise((resolve) => {
      const words = String(text).split(' ');
      let wordIdx = 0;
      targetEl.textContent = '';
      const cursor = document.createElement('span');
      cursor.className = 'typing-cursor';
      targetEl.appendChild(cursor);

      function typeNext() {
        if (wordIdx < words.length) {
          const currentText = words.slice(0, wordIdx + 1).join(' ');
          targetEl.textContent = currentText;
          targetEl.appendChild(cursor);
          wordIdx++;
          const t = setTimeout(typeNext, speedMs);
          activeMentorStreamingTimeouts.push(t);
        } else {
          cursor.remove();
          resolve();
        }
      }
      typeNext();
    });
  }

  async function renderMentorAdviceProgressive(data) {
    if (!mentorAdvisorContentEl) return;
    mentorAdvisorContentEl.style.display = 'flex';
    if (mentorStreamStatusEl) mentorStreamStatusEl.style.display = 'inline-flex';

    const shortTerm = data.short_term_tasks || [];
    const longTerm = data.long_term_tasks || [];
    const rationale = data.rationale || '';

    if (mentorAdviceShortTermEl) mentorAdviceShortTermEl.innerHTML = '';
    if (mentorAdviceLongTermEl) mentorAdviceLongTermEl.innerHTML = '';
    if (mentorAdviceRationaleEl) mentorAdviceRationaleEl.textContent = '';

    // Step 1: Stream Short Term Tasks
    if (mentorAdviceShortTermEl) {
      if (!shortTerm.length) {
        mentorAdviceShortTermEl.innerHTML = `
          <div class="mentor-task-card short-term-card">
            <span class="mentor-task-icon">🎯</span>
            <div class="mentor-task-body">
              <span class="mentor-task-text">No urgent tasks flagged right now — your current priorities look aligned.</span>
            </div>
          </div>`;
      } else {
        for (let i = 0; i < shortTerm.length; i++) {
          const taskText = shortTerm[i];
          const card = document.createElement('div');
          card.className = 'mentor-task-card short-term-card';
          
          const icon = document.createElement('span');
          icon.className = 'mentor-task-icon';
          icon.textContent = '⚡';

          const body = document.createElement('div');
          body.className = 'mentor-task-body';

          const textEl = document.createElement('div');
          textEl.className = 'mentor-task-text';

          body.appendChild(textEl);

          const actionBtn = document.createElement('button');
          actionBtn.type = 'button';
          actionBtn.className = 'mentor-task-action-btn';
          actionBtn.textContent = '+ Commit';
          actionBtn.title = 'Add this task directly to your commitments ledger';
          actionBtn.onclick = () => handleAddCommitmentFromAdvice(taskText, actionBtn);

          card.appendChild(icon);
          card.appendChild(body);
          card.appendChild(actionBtn);
          mentorAdviceShortTermEl.appendChild(card);

          await streamTextIntoElement(textEl, taskText, 14);
          await new Promise((r) => {
            const t = setTimeout(r, 60);
            activeMentorStreamingTimeouts.push(t);
          });
        }
      }
    }

    // Step 2: Stream Long Term Tasks
    if (mentorAdviceLongTermEl) {
      if (!longTerm.length) {
        mentorAdviceLongTermEl.innerHTML = `
          <div class="mentor-task-card long-term-card">
            <span class="mentor-task-icon">🚀</span>
            <div class="mentor-task-body">
              <span class="mentor-task-text">Not enough history yet to point toward a longer-term direction.</span>
            </div>
          </div>`;
      } else {
        for (let i = 0; i < longTerm.length; i++) {
          const taskText = longTerm[i];
          const card = document.createElement('div');
          card.className = 'mentor-task-card long-term-card';
          
          const icon = document.createElement('span');
          icon.className = 'mentor-task-icon';
          icon.textContent = '🚀';

          const body = document.createElement('div');
          body.className = 'mentor-task-body';

          const textEl = document.createElement('div');
          textEl.className = 'mentor-task-text';

          body.appendChild(textEl);

          const actionBtn = document.createElement('button');
          actionBtn.type = 'button';
          actionBtn.className = 'mentor-task-action-btn';
          actionBtn.textContent = '+ Agenda';
          actionBtn.title = 'Add this item to your rolling agenda for the next 1-on-1';
          actionBtn.onclick = () => handleAddAgendaFromAdvice(taskText, actionBtn);

          card.appendChild(icon);
          card.appendChild(body);
          card.appendChild(actionBtn);
          mentorAdviceLongTermEl.appendChild(card);

          await streamTextIntoElement(textEl, taskText, 14);
          await new Promise((r) => {
            const t = setTimeout(r, 60);
            activeMentorStreamingTimeouts.push(t);
          });
        }
      }
    }

    // Step 3: Stream Rationale
    if (mentorAdviceRationaleEl && rationale) {
      const rationaleSection = document.getElementById('section-rationale');
      if (rationaleSection) rationaleSection.style.display = '';
      await streamTextIntoElement(mentorAdviceRationaleEl, rationale, 10);
    } else {
      const rationaleSection = document.getElementById('section-rationale');
      if (rationaleSection) rationaleSection.style.display = 'none';
    }

    if (mentorStreamStatusEl) {
      mentorStreamStatusEl.style.display = 'none';
    }
  }

  async function handleAddCommitmentFromAdvice(text, btn) {
    if (!state.actingUserId) return;
    btn.disabled = true;
    try {
      const response = await fetch('/webhooks/ledgers/commitment', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify({ owner_user_id: state.actingUserId, description: text })
      });
      if (!response.ok) throw new Error('Failed to add commitment');
      btn.textContent = '✓ Committed';
      btn.classList.add('added');
      showToast('Added task to Commitments ledger!', 'success');
      addLog(`Mentor task logged to commitments: "${text.slice(0, 40)}…"`, 'success');
      fetchLedgers();
    } catch (err) {
      btn.disabled = false;
      showToast(`Couldn't log commitment: ${err.message}`, 'error');
    }
  }

  async function handleAddAgendaFromAdvice(text, btn) {
    if (!state.actingUserId) return;
    btn.disabled = true;
    try {
      const response = await fetch('/webhooks/agenda-trigger', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify({
          trigger_type: 'manual_note',
          report_user_id: state.reportUserId || state.actingUserId,
          acting_user_id: state.actingUserId,
          text: text,
          visibility: 'shared'
        })
      });
      if (!response.ok) throw new Error('Failed to add to agenda');
      btn.textContent = '✓ On Agenda';
      btn.classList.add('added');
      showToast('Added topic to Rolling Agenda!', 'success');
      addLog(`Mentor growth goal added to agenda: "${text.slice(0, 40)}…"`, 'success');
      fetchPayload();
    } catch (err) {
      btn.disabled = false;
      showToast(`Couldn't add to agenda: ${err.message}`, 'error');
    }
  }

  function copyMentorAdvice() {
    if (!state.lastMentorAdvice) return;
    const { short_term_tasks = [], long_term_tasks = [], rationale = '' } = state.lastMentorAdvice;
    const shortText = short_term_tasks.map((t, idx) => `${idx + 1}. ${t}`).join('\n');
    const longText = long_term_tasks.map((t, idx) => `${idx + 1}. ${t}`).join('\n');
    
    const formatted = `### 🧭 Mentor Strategic Guidance\n\n#### 🎯 This Week's Focus (Immediate Tasks)\n${shortText || 'No urgent tasks'}\n\n#### 🚀 Strategic Growth (Quarterly Horizon)\n${longText || 'No strategic goals'}\n\n#### 💡 Mentor Rationale\n${rationale || 'None'}`;
    
    navigator.clipboard.writeText(formatted)
      .then(() => showToast('Copied mentor advice to clipboard!', 'success'))
      .catch(() => showToast('Could not copy to clipboard.', 'error'));
  }

  function viewDossier(dossierId) {
    closeMeetingsPanel();
    switchPage('dossiers');
    // fetchDossiers() (triggered by switchPage) re-renders the list
    // async — the target card may not exist in the DOM yet on the very
    // next frame, so give it a moment before scrolling/highlighting.
    setTimeout(() => {
      const cardEl = document.getElementById(`dossier-card-${dossierId}`);
      if (!cardEl) return;
      cardEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      cardEl.classList.add('agenda-item-card-highlight');
      setTimeout(() => cardEl.classList.remove('agenda-item-card-highlight'), 2000);
    }, 300);
  }

  async function completeChecklistItem(itemType, itemId, checkboxEl) {
    if (!state.actingUserId || !state.sharedToken || !state.userSecret) return;

    // 1. Instant feedback on checkbox
    checkboxEl.disabled = true;
    checkboxEl.classList.add('checked');
    checkboxEl.textContent = '✓';

    // 2. Optimistically update local checklist data
    let optimisticItem = null;
    if (state.checklistData && state.checklistData.pending) {
      const idx = state.checklistData.pending.findIndex(
        (it) => it.item_type === itemType && it.item_id === itemId
      );
      if (idx !== -1) {
        optimisticItem = state.checklistData.pending.splice(idx, 1)[0];
        const resolvedItem = {
          ...optimisticItem,
          source: 'manual',
          ai_response: 'Saving…'
        };
        state.checklistData.resolved = state.checklistData.resolved || [];
        state.checklistData.resolved.unshift(resolvedItem);
        if (state.checklistData.stats) {
          state.checklistData.stats.done = Math.min(
            (state.checklistData.stats.done || 0) + 1,
            state.checklistData.stats.total || 1
          );
        }
      }
    }

    // 3. Immediately re-render with the optimistic state
    if (optimisticItem) {
      renderChecklist(state.checklistData);
    }

    // 4. Background network call to complete and generate AI message
    try {
      const response = await fetch('/webhooks/checklist/complete', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify({
          owner_user_id: state.actingUserId,
          item_type: itemType,
          item_id: itemId
        })
      });

      if (!response.ok) throw new Error('complete failed');
      const data = await response.json();

      // Update the resolved item's real AI response and stats
      if (state.checklistData && state.checklistData.resolved) {
        const target = state.checklistData.resolved.find(
          (it) => it.item_type === itemType && it.item_id === itemId
        );
        if (target) {
          target.ai_response = data.ai_response || '';
        }
        if (data.stats && state.checklistData.stats) {
          state.checklistData.stats = data.stats;
        }
        renderChecklist(state.checklistData);
      }

      showToast(data.ai_response || 'Nice work.', 'success');
      addLog(`Checklist item done: "${optimisticItem ? optimisticItem.title : itemId}"`, 'success');
    } catch (err) {
      // Revert & sync with server on failure
      await fetchChecklist();
      showToast("Couldn't check that off — try again.", 'error');
    }
  }

  function renderAccountIndicator() {
    // No role label here anymore — this is always your own account
    // indicator now, not a persona you picked at login. Whether you're
    // currently viewing your own or a report's agenda is shown by the
    // Rolling Agenda tab's own picker instead (renderAgendaReportPicker).
    accountLabel.textContent = state.displayName || state.loginEmail;
    renderAgendaReportPicker();
  }

  // On-demand refetch of state.managedReports — GET /webhooks/managed-
  // reports, the same shape /webhooks/login returns. Called on the
  // live-notifications SSE stream's own managed_reports_changed event
  // (REAL CHANGE, requested: fix the gap where a newly-synced Pair only
  // ever showed up in the picker on the manager's NEXT login) — no more
  // logging out and back in just to see a new report.
  async function fetchManagedReports() {
    if (!state.actingUserId || !state.sharedToken || !state.userSecret) return;
    try {
      const response = await fetch(
        `/webhooks/managed-reports?owner_user_id=${encodeURIComponent(state.actingUserId)}`,
        {
          headers: {
            'X-Agenda-Token': state.sharedToken,
            'X-Acting-User-Secret': state.userSecret
          }
        }
      );
      if (response.ok) {
        const data = await response.json();
        state.managedReports = data.managed_reports || [];
        persistSession();
        renderAgendaReportPicker();
      }
    } catch {
      // Offline/standalone mode — leave whatever's already rendered
      // showing, same fallback posture as fetchGoals/fetchDossiers above.
    }
  }

  // The Rolling Agenda tab's own report picker — only shown at all when
  // you manage at least one report (state.managedReports non-empty). No
  // "Myself" option — see this function's own body comment.
  function renderAgendaReportPicker() {
    if (!agendaReportPickerRowEl || !pairSwitcher) return;
    if (state.managedReports.length === 0) {
      agendaReportPickerRowEl.style.display = 'none';
      return;
    }
    // No "Myself" option — REAL CHANGE (requested: "the switch contains
    // only the reports (no Myself)"). The Rolling Agenda tab is always
    // showing a specific report's shared agenda, never a personal one of
    // the manager's own — if state.reportUserId isn't (yet, or no
    // longer) one of the managed reports, default to the first one
    // rather than falling back to a "Myself" view that no longer exists
    // in this switcher.
    if (!state.managedReports.some((r) => r.report_user_id === state.reportUserId)) {
      // selectAgendaReport doesn't re-render this picker itself — fall
      // through below to build the options against the now-corrected
      // state.reportUserId instead of returning early.
      selectAgendaReport(state.managedReports[0].report_user_id);
    }
    const options = state.managedReports.map((r) => `
        <option value="${r.report_user_id}" ${r.report_user_id === state.reportUserId ? 'selected' : ''}>
          ${escapeHtml(r.display_name || r.report_user_id)}
        </option>`);
    pairSwitcher.innerHTML = options.join('');
    agendaReportPickerRowEl.style.display = 'flex';
  }

  // Preferences (POST /webhooks/update-preferences) — self-service editing
  // of an auto-provisioned user's schedule settings (tz/pulse-fire-time/
  // late-cutoff), the one thing Notion auto-provisioning (app/ingest/
  // notion_user_provision.py) can only guess org-wide defaults for. Every
  // field is optional/blank-by-default here: only fields the user actually
  // fills in get sent, so opening and closing the modal without typing
  // anything is a no-op, not an accidental reset to empty values.
  function setupPreferences() {
    btnPreferences.addEventListener('click', () => {
      preferencesError.style.display = 'none';
      preferencesSuccess.style.display = 'none';
      prefTzInput.value = '';
      prefPulseTimeInput.value = '';
      prefCutoffTimeInput.value = '';
      preferencesOverlay.style.display = 'flex';
      fetchGoogleConnectStatus();
    });
    btnPreferencesCancel.addEventListener('click', () => {
      preferencesOverlay.style.display = 'none';
    });
    preferencesOverlay.addEventListener('click', (e) => {
      if (e.target === preferencesOverlay) preferencesOverlay.style.display = 'none';
    });
    formPreferences.addEventListener('submit', handleSavePreferences);
    btnGoogleDisconnect?.addEventListener('click', handleGoogleDisconnect);
  }

  // Self-service Google OAuth connect (app/triggers/google_oauth_router.py)
  // — lets this user link their own Gmail/Calendar/Docs instead of the
  // whole deployment sharing one account. /start needs query-param auth
  // (same reasoning as the live-notifications SSE URL below): it's reached
  // via a plain <a href> navigation, which can't set custom headers.
  async function fetchGoogleConnectStatus() {
    if (!googleConnectLoadingEl) return;
    googleConnectLoadingEl.style.display = '';
    googleConnectDisconnectedEl.style.display = 'none';
    googleConnectConnectedEl.style.display = 'none';

    if (linkGoogleConnect) {
      linkGoogleConnect.href = `/webhooks/google-oauth/start?owner_user_id=${encodeURIComponent(state.actingUserId)}`
        + `&token=${encodeURIComponent(state.sharedToken)}`
        + `&secret=${encodeURIComponent(state.userSecret)}`;
    }

    try {
      const response = await fetch(
        `/webhooks/google-oauth/status?owner_user_id=${encodeURIComponent(state.actingUserId)}`,
        {
          headers: {
            'X-Agenda-Token': state.sharedToken,
            'X-Acting-User-Secret': state.userSecret
          }
        }
      );
      if (!response.ok) throw new Error('status check failed');
      const data = await response.json();
      googleConnectLoadingEl.style.display = 'none';
      if (data.connected) {
        googleConnectEmailEl.textContent = data.google_email || '(email unknown)';
        googleConnectConnectedEl.style.display = '';
      } else {
        googleConnectDisconnectedEl.style.display = '';
      }
    } catch {
      googleConnectLoadingEl.textContent = 'Could not check connection status.';
    }
  }

  async function handleGoogleDisconnect() {
    btnGoogleDisconnect.disabled = true;
    try {
      await fetch('/webhooks/google-oauth/disconnect', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify({ owner_user_id: state.actingUserId })
      });
    } catch {
      // fetchGoogleConnectStatus below re-checks either way — a network
      // error here just means the next status check still shows
      // "connected", which is honest (we don't actually know it worked).
    } finally {
      btnGoogleDisconnect.disabled = false;
      fetchGoogleConnectStatus();
    }
  }

  async function handleSavePreferences(e) {
    e.preventDefault();
    preferencesError.style.display = 'none';
    preferencesSuccess.style.display = 'none';

    const body = { acting_user_id: state.actingUserId };
    if (prefTzInput.value.trim()) body.tz = prefTzInput.value.trim();
    if (prefPulseTimeInput.value) body.pulse_fire_time_local = prefPulseTimeInput.value;
    if (prefCutoffTimeInput.value) body.late_cutoff_local = prefCutoffTimeInput.value;

    if (!body.tz && !body.pulse_fire_time_local && !body.late_cutoff_local) {
      preferencesOverlay.style.display = 'none';
      return;
    }

    preferencesSaveBtnText.style.display = 'none';
    preferencesSaveSpinner.style.display = '';
    btnPreferencesSave.disabled = true;

    try {
      const response = await fetch('/webhooks/update-preferences', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify(body)
      });
      const data = await response.json();
      if (!response.ok) {
        preferencesError.textContent = data.reason || 'Could not save preferences.';
        preferencesError.style.display = '';
        return;
      }
      preferencesSuccess.style.display = '';
      setTimeout(() => { preferencesOverlay.style.display = 'none'; }, 1200);
    } catch {
      preferencesError.textContent = 'Network error — could not reach the server.';
      preferencesError.style.display = '';
    } finally {
      preferencesSaveBtnText.style.display = '';
      preferencesSaveSpinner.style.display = 'none';
      btnPreferencesSave.disabled = false;
    }
  }

  // Page Tabs (Rolling Agenda vs Ledgers) — a separate top-level page,
  // not mixed into the rolling-agenda columns, per explicit request.
  function setupPageTabs() {
    pageTabAgenda.addEventListener('click', () => switchPage('agenda'));
    pageTabLedgers.addEventListener('click', () => switchPage('ledgers'));
    pageTabDossiers.addEventListener('click', () => switchPage('dossiers'));
    pageTabGraph.addEventListener('click', () => switchPage('graph'));
    pageTabChat.addEventListener('click', () => switchPage('chat'));
  }

  function switchPage(page) {
    state.currentPage = page;
    pageAgenda.style.display = page === 'agenda' ? '' : 'none';
    pageLedgers.style.display = page === 'ledgers' ? '' : 'none';
    pageDossiers.style.display = page === 'dossiers' ? '' : 'none';
    pageGraph.style.display = page === 'graph' ? '' : 'none';
    pageChat.style.display = page === 'chat' ? '' : 'none';

    [
      [pageTabAgenda, 'agenda'],
      [pageTabLedgers, 'ledgers'],
      [pageTabDossiers, 'dossiers'],
      [pageTabGraph, 'graph'],
      [pageTabChat, 'chat']
    ].forEach(([tab, name]) => {
      const active = name === page;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });

    if (page === 'ledgers') { fetchLedgers(); populateGoalPicker(); }
    if (page === 'dossiers') fetchDossiers();
    if (page === 'graph') {
      fetchGraph();
      if (graphInstance) {
        setTimeout(() => {
          graphInstance.resize();
          graphInstance.fit(undefined, 40);
        }, 50);
      }
    }
    if (page === 'chat') chatInput.focus();
  }

  // Ledgers (Commitments & Accomplishments) — GET /webhooks/ledgers.
  // Always state.actingUserId (self), not the Rolling Agenda tab's
  // switchable state.reportUserId — a ledger is private, always (same
  // rule Friday-review's own skill spec states), not something a
  // manager casually browses for a report via a generic UI toggle.
  async function fetchLedgers() {
    try {
      const url = `/webhooks/ledgers?report_user_id=${state.actingUserId}&acting_user_id=${state.actingUserId}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (response.ok) {
        const data = await response.json();
        renderCommitments(data.commitments || []);
        renderAccomplishments(data.accomplishments || []);
        return data;
      }
    } catch {
      // Offline/standalone mode — leave the empty states showing.
    }
    return { commitments: [], accomplishments: [] };
  }

  function renderCommitments(commitments) {
    if (!commitmentsListEl) return;
    if (!commitments.length) {
      commitmentsListEl.innerHTML = '';
      commitmentsListEl.appendChild(commitmentsEmptyStateEl);
      commitmentsEmptyStateEl.style.display = 'flex';
      return;
    }
    commitmentsEmptyStateEl.style.display = 'none';
    commitmentsListEl.innerHTML = commitments.map((c) => {
      const due = c.due_at ? new Date(c.due_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : null;
      return `
        <div class="agenda-item-card">
          <div class="item-main-row">
            <div class="item-content">
              <div class="item-text-input" style="cursor: default;">${escapeHtml(c.description || '')}</div>
              <div class="item-meta-row">
                <span class="badge">${escapeHtml(c.status || 'open')}</span>
                ${due ? `<span class="badge">due ${due}</span>` : ''}
              </div>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  function renderAccomplishments(accomplishments) {
    if (!accomplishmentsListEl) return;
    if (!accomplishments.length) {
      accomplishmentsListEl.innerHTML = '';
      accomplishmentsListEl.appendChild(accomplishmentsEmptyStateEl);
      accomplishmentsEmptyStateEl.style.display = 'flex';
      return;
    }
    accomplishmentsEmptyStateEl.style.display = 'none';
    accomplishmentsListEl.innerHTML = accomplishments.map((a) => {
      const date = a.occurred_at ? new Date(a.occurred_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : '';
      return `
        <div class="agenda-item-card">
          <div class="item-main-row">
            <div class="item-content">
              <div class="item-text-input" style="cursor: default;">${escapeHtml(a.description || '')}</div>
              <div class="item-meta-row">
                ${date ? `<span class="badge">${date}</span>` : ''}
                ${a.goal_title ? `<span class="badge">🎯 ${escapeHtml(a.goal_title)}</span>` : ''}
              </div>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  // Self-only, same reasoning as fetchLedgers above — the accomplishment
  // goal picker only ever offers the acting user's OWN goals, never the
  // Rolling Agenda tab's switchable report_user_id.
  async function populateGoalPicker() {
    if (!accomplishmentGoalSelect || !state.actingUserId) return;
    try {
      const url = `/webhooks/goals?report_user_id=${state.actingUserId}&acting_user_id=${state.actingUserId}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (!response.ok) return;
      const data = await response.json();
      const goals = data.goals || [];
      const typeLabels = { objective: 'Obj', key_result: 'KR', career_goal: 'Career' };
      accomplishmentGoalSelect.innerHTML = '<option value="">No linked goal</option>' + goals.map((g) => {
        const label = typeLabels[g.goal_type] || g.goal_type;
        return `<option value="${escapeHtml(g.id)}">[${label}] ${escapeHtml(g.title || '')}</option>`;
      }).join('');
    } catch {
      // Offline/standalone — leave just "No linked goal", same fallback
      // posture as every other fetch* here.
    }
  }

  function setupLedgerForms() {
    if (formAddCommitment) formAddCommitment.addEventListener('submit', handleAddCommitment);
    if (formAddAccomplishment) formAddAccomplishment.addEventListener('submit', handleAddAccomplishment);
  }

  async function handleAddCommitment(e) {
    e.preventDefault();
    const description = commitmentDescriptionInput.value.trim();
    if (!description || !state.actingUserId) return;
    btnAddCommitment.disabled = true;
    try {
      const body = { owner_user_id: state.actingUserId, description };
      if (commitmentDueAtInput.value) {
        body.due_at = new Date(commitmentDueAtInput.value).toISOString();
      }
      const response = await fetch('/webhooks/ledgers/commitment', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error('add commitment failed');
      commitmentDescriptionInput.value = '';
      commitmentDueAtInput.value = '';
      showToast('Commitment added.', 'success');
      await fetchLedgers();
    } catch {
      showToast("Couldn't add that commitment — try again.", 'error');
    } finally {
      btnAddCommitment.disabled = false;
    }
  }

  async function handleAddAccomplishment(e) {
    e.preventDefault();
    const description = accomplishmentDescriptionInput.value.trim();
    if (!description || !state.actingUserId) return;
    btnAddAccomplishment.disabled = true;
    try {
      const body = { owner_user_id: state.actingUserId, description };
      if (accomplishmentGoalSelect.value) {
        body.goal_id = accomplishmentGoalSelect.value;
      }
      const response = await fetch('/webhooks/ledgers/accomplishment', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error('add accomplishment failed');
      accomplishmentDescriptionInput.value = '';
      accomplishmentGoalSelect.value = '';
      showToast('Accomplishment logged.', 'success');
      await fetchLedgers();
    } catch {
      showToast("Couldn't log that accomplishment — try again.", 'error');
    } finally {
      btnAddAccomplishment.disabled = false;
    }
  }

  // Agent Chat — talks to ADK's own /run endpoint (POST) against this
  // process's root_agent, and /apps/{app}/users/{user}/sessions to create
  // a session first. These are core ADK routes mounted by
  // get_fast_api_app(web=True) in app/fast_api_app.py, unauthenticated
  // like the rest of the ADK dev server — same as `adk web` itself.
  function setupChat() {
    formChat.addEventListener('submit', handleChatSubmit);
    btnChatReset.addEventListener('click', resetChatSession);
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        formChat.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      }
    });
    chatInput.addEventListener('input', () => {
      chatInput.style.height = 'auto';
      chatInput.style.height = `${Math.min(chatInput.scrollHeight, 120)}px`;
    });
  }

  function resetChatSession() {
    chatState.sessionId = null;
    chatSessionEyebrow.textContent = 'No session yet';
    chatMessagesEl.innerHTML = '';
    chatMessagesEl.appendChild(chatEmptyStateEl);
    chatEmptyStateEl.style.display = 'block';
    addLog('Agent chat session reset', 'info');
  }

  async function ensureChatSession() {
    if (chatState.sessionId) return chatState.sessionId;
    const response = await fetch(
      `/apps/${encodeURIComponent(chatState.appName)}/users/${encodeURIComponent(chatState.userId)}/sessions`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }
    );
    if (!response.ok) {
      throw new Error(`Could not start a session (${response.status})`);
    }
    const session = await response.json();
    chatState.sessionId = session.id;
    chatSessionEyebrow.textContent = `Session ${session.id.slice(0, 8)}…`;
    return chatState.sessionId;
  }

  async function handleChatSubmit(e) {
    e.preventDefault();
    if (chatState.sending) return;
    const text = chatInput.value.trim();
    if (!text) return;

    appendChatMessage('user', text);
    chatInput.value = '';
    chatInput.style.height = 'auto';

    chatState.sending = true;
    chatSendText.style.display = 'none';
    chatSendSpinner.style.display = 'inline-block';
    btnChatSend.disabled = true;
    const typingEl = appendChatTyping();

    try {
      const sessionId = await ensureChatSession();
      const response = await fetch('/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          app_name: chatState.appName,
          user_id: chatState.userId,
          session_id: sessionId,
          new_message: { role: 'user', parts: [{ text }] },
          streaming: false
        })
      });
      const events = await response.json().catch(() => null);
      typingEl.remove();

      if (!response.ok) {
        const detail = (events && events.detail) ? events.detail : `HTTP ${response.status}`;
        appendChatMessage('agent', `Error: ${detail}`, true);
        addLog(`Agent chat error: ${detail}`, 'error');
        return;
      }
      renderAgentEvents(Array.isArray(events) ? events : []);
      addLog('Agent chat: message sent to root_agent', 'signal');
    } catch (err) {
      typingEl.remove();
      appendChatMessage('agent', `Couldn't reach the agent — ${err.message}`, true);
      addLog(`Agent chat failed: ${err.message}`, 'error');
    } finally {
      chatState.sending = false;
      chatSendText.style.display = '';
      chatSendSpinner.style.display = 'none';
      btnChatSend.disabled = false;
      chatInput.focus();
    }
  }

  // Events come back as ADK Event objects (camelCase-aliased): each has
  // `author` ('user' or an agent/tool name) and `content.parts`, where a
  // part may carry `text`, a `functionCall`, or a `functionResponse`. The
  // final assistant reply is usually the last part with author !== 'user'
  // and a text part, but tool calls are rendered too so a pulse trigger
  // is visibly traceable, not just its final text.
  function renderAgentEvents(events) {
    let sawText = false;
    events.forEach((event) => {
      if (event.author === 'user') return;
      const parts = (event.content && event.content.parts) || [];
      parts.forEach((part) => {
        if (part.text) {
          appendChatMessage('agent', part.text);
          sawText = true;
        } else if (part.functionCall) {
          appendChatToolTrace(`→ calling ${part.functionCall.name}(${JSON.stringify(part.functionCall.args || {})})`);
        } else if (part.functionResponse) {
          appendChatToolTrace(`← ${part.functionResponse.name} returned ${JSON.stringify(part.functionResponse.response || {})}`);
        }
      });
    });
    if (!sawText) {
      appendChatMessage('agent', '(no text response — see tool trace above)', false);
    }
  }

  // Agent replies are markdown (the model writes **bold**, lists, code
  // fences, ...) but were rendered via textContent, so every reply showed
  // up as literal asterisks/backticks instead of formatted text. User
  // messages stay plain text (they're never markdown, and this avoids
  // marked/DOMPurify roundtripping something the user typed verbatim).
  // marked+DOMPurify are loaded in index.html; fall back to textContent
  // if either failed to load (offline dev, CDN blocked, etc).
  function appendChatMessage(role, text, isError = false) {
    chatEmptyStateEl.style.display = 'none';
    const bubble = document.createElement('div');
    bubble.className = `chat-msg chat-msg-${role === 'user' ? 'user' : 'agent'}${isError ? ' chat-msg-error' : ''}`;
    if (role !== 'user' && window.marked && window.DOMPurify) {
      bubble.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text));
    } else {
      bubble.textContent = text;
    }
    chatMessagesEl.appendChild(bubble);
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
    return bubble;
  }

  function appendChatToolTrace(text) {
    chatEmptyStateEl.style.display = 'none';
    const el = document.createElement('div');
    el.className = 'chat-msg chat-msg-tool';
    el.textContent = text;
    chatMessagesEl.appendChild(el);
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
    return el;
  }

  function appendChatTyping() {
    chatEmptyStateEl.style.display = 'none';
    const el = document.createElement('div');
    el.className = 'chat-typing';
    el.innerHTML = '<span></span><span></span><span></span>';
    chatMessagesEl.appendChild(el);
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
    return el;
  }

  // Event Listener Setup
  function setupEventListeners() {
    btnRefresh.addEventListener('click', () => {
      fetchPayload(true);
      showToast('Refreshed agenda items', 'success');
      addLog('Manually refreshed agenda', 'info');
    });

    formAddNote.addEventListener('submit', handleAddNote);
    formTriggerMeeting.addEventListener('submit', handleRunMeeting);
    if (btnStopMeeting) btnStopMeeting.addEventListener('click', stopMeetingPipeline);

    if (btnSimJira) btnSimJira.addEventListener('click', () => simulateSignal('jira', 'JIRA-881: Database connection pool exhaustion under load'));
    if (btnSimSlack) btnSimSlack.addEventListener('click', () => simulateSignal('slack', 'Slack Question: Clarification on OAuth scope requirements'));
    if (btnSimAccomplishment) btnSimAccomplishment.addEventListener('click', () => simulateSignal('accomplishment', 'Completed L2 Notion manager-report sync component'));

    // Clear log button
    if (btnClearLog) {
      btnClearLog.addEventListener('click', () => {
        if (logEntriesEl) {
          logEntriesEl.innerHTML = '';
          if (logEmptyState) {
            logEntriesEl.appendChild(logEmptyState);
            logEmptyState.style.display = 'flex';
          }
        }
      });
    }

    // Keyboard shortcut: Cmd/Ctrl+Enter to submit note
    if (noteTextInput && formAddNote) {
      noteTextInput.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
          formAddNote.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
        }
      });
    }

    if (agentBubblePlayEl) agentBubblePlayEl.addEventListener('click', playAgentBubbleAudio);
    if (agentBubbleDismissEl) agentBubbleDismissEl.addEventListener('click', hideAgentBubble);
    if (agentBubbleChecklistEl) {
      agentBubbleChecklistEl.addEventListener('click', () => {
        hideAgentBubble();
        openChecklistPanel();
      });
    }
  }

  // Visibility Selector (button group replacing select)
  function setupVisibilitySelector() {
    const visOptions = document.querySelectorAll('.vis-option');
    visOptions.forEach(btn => {
      btn.addEventListener('click', () => {
        visOptions.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        state.selectedVisibility = btn.dataset.value;
        noteVisibilityHidden.value = btn.dataset.value;
      });
    });
  }

  // Char counter for textarea
  function setupCharCounter() {
    noteTextInput.addEventListener('input', () => {
      const len = noteTextInput.value.length;
      const max = 300;
      charCounter.textContent = `${len} / ${max}`;
      charCounter.classList.remove('warn', 'danger');
      if (len > max * 0.9) charCounter.classList.add('danger');
      else if (len > max * 0.7) charCounter.classList.add('warn');
    });
  }

  // Real backend calls — every mutating action in this UI (resolve,
  // keep/drop, field edit, visibility change, meeting synthesis) goes
  // through these, not just local state. A dashboard that *looks* like
  // it saved your change but silently didn't is worse than no dashboard.
  async function postSignal(signalType, itemId, changes) {
    const response = await fetch('/webhooks/agenda-signal', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Agenda-Token': state.sharedToken,
        'X-Acting-User-Secret': state.userSecret
      },
      body: JSON.stringify({
        signal_type: signalType,
        report_user_id: state.reportUserId,
        acting_user_id: state.actingUserId,
        item_id: itemId,
        changes: changes || undefined
      })
    });
    if (!response.ok) {
      throw new Error(`agenda-signal ${signalType} failed: ${response.status}`);
    }
    const result = await response.json();
    if (result.status === 'rejected') {
      throw new Error(result.reason || 'rejected');
    }
    return result;
  }

  async function postTrigger(triggerType, extra) {
    const response = await fetch('/webhooks/agenda-trigger', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Agenda-Token': state.sharedToken,
        'X-Acting-User-Secret': state.userSecret
      },
      body: JSON.stringify({
        trigger_type: triggerType,
        report_user_id: state.reportUserId,
        acting_user_id: state.actingUserId,
        ...extra
      })
    });
    if (!response.ok && response.status !== 202) {
      throw new Error(`agenda-trigger ${triggerType} failed: ${response.status}`);
    }
    // meeting_end returns 202 {"status": "accepted"}; everything else
    // returns 200 with either {"status": "ok", ...} or a data-modeled
    // rejection ({"status": "rejected", "reason": ...}) — a 200 is
    // `response.ok`, so that rejection has to be checked explicitly or
    // it silently looks like success.
    const body = await response.json().catch(() => ({}));
    if (body.status === 'rejected') {
      throw new Error(body.reason || 'rejected');
    }
    return { httpStatus: response.status, body };
  }

  // GET /webhooks/agenda-trigger-status — the real-outcome counterpart to
  // postTrigger's 202 for meeting_end (see that route's own docstring):
  // "pending" while the background pipeline is still running, or the
  // real terminal outcome (ok/rejected/error) once known. Returns null
  // on a network failure — callers already have their own polling loop
  // and next-tick retry, no need to duplicate that here.
  async function fetchTriggerStatus(meetingId) {
    try {
      const url = `/webhooks/agenda-trigger-status?meeting_id=${encodeURIComponent(meetingId)}&acting_user_id=${encodeURIComponent(state.actingUserId)}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (!response.ok) return null;
      return await response.json();
    } catch {
      return null;
    }
  }

  // True while the user has an item's text field focused — the 4s poll
  // must not blow away in-progress typing or an item that was just
  // resolved/dropped and hasn't round-tripped back from the server yet.
  function isUserEditing() {
    const active = document.activeElement;
    return !!(
      active &&
      (active.classList.contains('item-text-input') || active === noteTextInput)
    );
  }

  // Check backend connectivity
  async function checkBackendStatus() {
    try {
      const resp = await fetch('/webhooks/agenda-payload?report_user_id=_&acting_user_id=_', {
        headers: { 'X-Agenda-Token': state.sharedToken, 'X-Acting-User-Secret': '_' }
      });
      if (resp.ok || resp.status === 401 || resp.status === 422) {
        if (hintDot) hintDot.className = 'hint-dot online';
        if (backendStatusText) backendStatusText.textContent = 'Backend reachable';
      } else {
        throw new Error('non-ok');
      }
    } catch {
      if (hintDot) hintDot.className = 'hint-dot offline';
      if (backendStatusText) backendStatusText.textContent = 'Running in offline mode';
    }
  }

  // Activity Log helper
  function addLog(message, type = 'info') {
    if (logEmptyState) logEmptyState.style.display = 'none';
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const dotClass = type === 'success' ? 'log-dot-success'
      : type === 'signal' ? 'log-dot-signal'
      : type === 'error' ? 'log-dot-error'
      : 'log-dot-info';
    entry.innerHTML = `
      <div class="log-dot ${dotClass}"></div>
      <div class="log-body">
        <div class="log-msg">${escapeHtml(message)}</div>
        <div class="log-time">${timeStr}</div>
      </div>`;
    logEntriesEl.prepend(entry);
    // Keep max 50 entries
    const entries = logEntriesEl.querySelectorAll('.log-entry');
    if (entries.length > 50) entries[entries.length - 1].remove();
  }

  // /webhooks/agenda-payload returns build_a2ui_payload's shape:
  // {"components": [...]} — paired editable_text/visibility_toggle
  // entries per open item (joined by item_id), consent_card entries for
  // pending-consent items, plus a trailing add_item marker. Not a flat
  // {items, stale_items} list — reconstruct one from the components.
  function parsePayloadComponents(components) {
    const itemsById = {};
    const staleItems = [];
    (components || []).forEach((c) => {
      if (c.type === 'editable_text') {
        itemsById[c.item_id] = itemsById[c.item_id] || { id: c.item_id };
        itemsById[c.item_id].text = c.text;
        itemsById[c.item_id].source = c.source;
      } else if (c.type === 'visibility_toggle') {
        itemsById[c.item_id] = itemsById[c.item_id] || { id: c.item_id };
        itemsById[c.item_id].visibility = c.current;
      } else if (c.type === 'consent_card') {
        staleItems.push({
          id: c.item_id,
          text: c.text,
          source: 'manual',
          source_link: null,
          visibility: 'shared',
          status: 'pending_consent',
          surfaced_count: 5,
          created_by_role: 'report'
        });
      }
    });
    const items = Object.values(itemsById).map((it) => ({
      id: it.id,
      text: it.text || '',
      source: it.source || 'manual',
      source_link: null,
      visibility: it.visibility || 'shared',
      status: 'open',
      surfaced_count: 0,
      created_by_role: 'report'
    }));
    return { items, staleItems };
  }

  // Fetch Payload from API Endpoint (with fallback)
  async function fetchPayload(forceToast = false) {
    if (isUserEditing()) {
      // Skip this refresh rather than clobbering an in-progress edit —
      // applies regardless of forceToast (a manual refresh or the
      // meeting-flow poll loop must not blow away an in-progress edit
      // in some OTHER item's text field either); the next opportunity
      // picks up whatever the user settles on once they blur the field.
      return;
    }
    state.isSyncing = true;
    try {
      const url = `/webhooks/agenda-payload?report_user_id=${state.reportUserId}&acting_user_id=${state.actingUserId}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });

      if (response.ok) {
        const data = await response.json();
        const { items, staleItems } = parsePayloadComponents(data.components);
        state.agendaItems = filterItemsByVisibility(items);
        state.staleItems = filterItemsByVisibility(staleItems);
      }
    } catch (err) {
      // Endpoint fallback mode if running standalone frontend without backend header auth
      console.log('Using local fallback state for UI display');
    } finally {
      state.isSyncing = false;
      render();
    }
  }

  // Objectives & Key Results (Notion-sourced) — GET /webhooks/goals
  // reads this app's own DB (already ingested via `mentor seed --live`),
  // not a live Notion call per page-load. Always state.actingUserId
  // (self), not the Rolling Agenda tab's switchable state.reportUserId —
  // REAL CHANGE (requested: "a manager can't see okrs and goals of
  // their reports and vice versa"), same private-always rule fetchLedgers
  // already follows. The backend enforces this too (goals_webhook now
  // rejects report_user_id != acting_user_id); this just keeps the UI
  // from ever asking for something it'd be refused anyway.
  async function fetchGoals() {
    try {
      const url = `/webhooks/goals?report_user_id=${state.actingUserId}&acting_user_id=${state.actingUserId}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (response.ok) {
        const data = await response.json();
        const goals = data.goals || [];
        renderGoals(goals);
        return goals;
      }
    } catch {
      // Offline/standalone mode — leave the empty state showing, same
      // fallback posture as fetchPayload above.
    }
    return [];
  }

  function renderGoals(goals) {
    if (!goalsListEl) return;

    const objectives = goals.filter((g) => g.goal_type === 'objective');
    const keyResultsByObjective = {};
    goals
      .filter((g) => g.goal_type === 'key_result')
      .forEach((kr) => {
        const parent = kr.parent_external_id || 'unlinked';
        (keyResultsByObjective[parent] = keyResultsByObjective[parent] || []).push(kr);
      });
    const careerGoals = goals.filter((g) => g.goal_type === 'career_goal');

    const objectiveCards = objectives.map((obj) => {
      const krs = keyResultsByObjective[obj.external_id] || [];
      const krRows = krs.map((kr) => {
        const pct = kr.progress != null ? Math.round(kr.progress * 100) : null;
        return `
          <div class="item-meta-row" style="padding-left: 14px;">
            <span class="badge">${escapeHtml(kr.title || '')}</span>
            ${pct != null ? `<span class="badge">${pct}%</span>` : ''}
            ${kr.current_value != null && kr.target_value != null
              ? `<span class="badge">${kr.current_value} / ${kr.target_value}</span>`
              : ''}
          </div>`;
      }).join('');
      return `
        <div class="agenda-item-card">
          <div class="item-main-row">
            <div class="item-content">
              <div class="item-text-input" style="cursor: default;">${escapeHtml(obj.title || '')}</div>
              <div class="item-meta-row">
                ${obj.quarter ? `<span class="badge">${escapeHtml(obj.quarter)}</span>` : ''}
                ${obj.status ? `<span class="badge">${escapeHtml(obj.status)}</span>` : ''}
              </div>
              ${krRows}
            </div>
          </div>
        </div>`;
    }).join('');

    const careerGoalCards = careerGoals.map((cg) => `
      <div class="agenda-item-card">
        <div class="item-main-row">
          <div class="item-content">
            <div class="item-text-input" style="cursor: default;">${escapeHtml(cg.title || '')}</div>
            ${cg.status ? `<div class="item-meta-row"><span class="badge">${escapeHtml(cg.status)}</span></div>` : ''}
          </div>
        </div>
      </div>`).join('');

    // Rendered as two independent sections — Objectives & Key Results and
    // Career Goals each get their own list and empty state, rather than
    // one combined list distinguished only by a small badge.
    if (objectives.length) {
      goalsEmptyStateEl.style.display = 'none';
      goalsListEl.innerHTML = objectiveCards;
    } else {
      goalsListEl.innerHTML = '';
      goalsListEl.appendChild(goalsEmptyStateEl);
      goalsEmptyStateEl.style.display = 'flex';
    }

    if (!careerGoalsListEl) return;
    if (careerGoals.length) {
      careerGoalsEmptyStateEl.style.display = 'none';
      careerGoalsListEl.innerHTML = careerGoalCards;
    } else {
      careerGoalsListEl.innerHTML = '';
      careerGoalsListEl.appendChild(careerGoalsEmptyStateEl);
      careerGoalsEmptyStateEl.style.display = 'flex';
    }
  }

  // Last Meeting Notes (Notion "1:1 Notes" db) — GET /webhooks/one-on-one-notes
  async function fetchNotes() {
    try {
      const url = `/webhooks/one-on-one-notes?report_user_id=${state.reportUserId}&acting_user_id=${state.actingUserId}&limit=10`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (response.ok) {
        const data = await response.json();
        const notes = data.notes || [];
        renderNotes(notes);
        return notes;
      }
    } catch {
      // Offline/standalone mode — leave the empty state showing.
    }
    return [];
  }

  function renderNotes(notes) {
    if (!notesListEl) return;
    if (!notes.length) {
      notesListEl.innerHTML = '';
      notesListEl.appendChild(notesEmptyStateEl);
      notesEmptyStateEl.style.display = 'flex';
      return;
    }
    notesEmptyStateEl.style.display = 'none';

    notesListEl.innerHTML = notes.map((note) => {
      const date = note.created_at ? new Date(note.created_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : '';
      let linkedCount = 0;
      try {
        linkedCount = JSON.parse(note.linked_key_result_external_ids || '[]').length;
      } catch { /* not JSON, ignore */ }
      return `
        <div class="agenda-item-card">
          <div class="item-main-row">
            <div class="item-content">
              <div class="item-text-input" style="cursor: default;">${escapeHtml(note.title || '(untitled)')}</div>
              <div class="item-meta-row">
                ${date ? `<span class="badge">${date}</span>` : ''}
                ${linkedCount ? `<span class="badge">${linkedCount} Key Result${linkedCount > 1 ? 's' : ''} linked</span>` : ''}
              </div>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  // Pre-Meeting Dossiers (dossier flow) — GET /webhooks/dossier-payload
  // reads DossierDelivery rows fresh from this app's own DB on every
  // call (pull-based, same reasoning as fetchPayload's agenda-payload
  // route). Read-only: no editable_text/visibility_toggle/consent_card
  // handling, since dossiers aren't editable from this panel.
  //
  // owner_user_id must be state.actingUserId (the actually-logged-in
  // user — set at login, app/triggers/agenda_router.py's login_webhook's
  // own user_id), NOT state.reportUserId (whichever pair's REPORT side
  // is currently selected for the Agenda tab). DossierDelivery.owner_
  // user_id is scoped to whoever ran /mentor prep, which has nothing to
  // do with which agenda they're currently viewing — using reportUserId
  // here meant a manager viewing their report's agenda queried for the
  // REPORT's dossiers (usually none, and 401'd if that report never got
  // an agenda_client_secret) instead of their own.
  async function fetchDossiers() {
    try {
      const url = `/webhooks/dossier-payload?owner_user_id=${state.actingUserId}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (response.ok) {
        const data = await response.json();
        renderDossiers(data.components || []);
      }
    } catch {
      // Offline/standalone mode — leave the empty state showing, same
      // fallback posture as fetchGoals/fetchNotes above.
    }
  }

  function renderDossiers(components) {
    if (!dossiersListEl) return;
    const cards = components.filter((c) => c.type === 'dossier_card');
    if (!cards.length) {
      dossiersListEl.innerHTML = '';
      dossiersListEl.appendChild(dossiersEmptyStateEl);
      dossiersEmptyStateEl.style.display = 'flex';
      return;
    }
    dossiersEmptyStateEl.style.display = 'none';

    dossiersListEl.innerHTML = cards.map((card) => {
      const sentAt = card.sent_at
        ? new Date(card.sent_at).toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
        : 'not sent';
      return `
        <div class="agenda-item-card" id="dossier-card-${escapeHtml(card.item_id)}">
          <div class="item-main-row">
            <div class="item-content">
              <div class="item-text-input" style="cursor: default;">${escapeHtml(card.who || '(no attendee summary)')}</div>
              <p class="section-desc">${escapeHtml(card.why_now || '')}</p>
              <div class="item-meta-row">
                <span class="badge">${escapeHtml(sentAt)}</span>
                <button class="agent-bubble-play dossier-play-btn" type="button" data-delivery-id="${card.item_id}">▶ Play</button>
              </div>
            </div>
          </div>
        </div>`;
    }).join('');

    dossiersListEl.querySelectorAll('.dossier-play-btn').forEach((btn) => {
      btn.addEventListener('click', () => fetchAndPlayAudio('dossier', btn.dataset.deliveryId, btn));
    });
  }

  const sampleGraphData = {
    nodes: [
      { id: 'node-p1', label: 'Alex Chen', type: 'person', key: 'person:alex_chen', properties: { role: 'Engineering Lead', email: 'alex.chen@company.com' } },
      { id: 'node-p2', label: 'Sarah Connor', type: 'person', key: 'person:sarah_connor', properties: { role: 'Staff Product Manager', email: 'sarah@company.com' } },
      { id: 'node-id-slack', label: 'slack/U01928374 (@alex.chen)', type: 'identity', key: 'slack:U01928374', properties: { source: 'slack', external_id: 'U01928374', handle: '@alex.chen' } },
      { id: 'node-id-github', label: 'github/achen-dev', type: 'identity', key: 'github:achen-dev', properties: { source: 'github', username: 'achen-dev' } },
      { id: 'node-id-jira', label: 'jira/alex.chen', type: 'identity', key: 'jira:alex.chen', properties: { source: 'jira', username: 'alex.chen' } },
      { id: 'node-id-email', label: 'google/alex.chen@company.com', type: 'identity', key: 'calendar:alex.chen@company.com', properties: { source: 'google', email: 'alex.chen@company.com' } },
      { id: 'node-e1', label: 'Q3 Product Roadmap Sync', type: 'event', key: 'gcal:evt_892374823', properties: { calendar: 'Team Planning', date: '2026-08-28' } },
      { id: 'node-m1', label: 'Auth Token Architecture', type: 'message', key: 'slack:msg_982374923', properties: { channel: '#eng-architecture' } },
      { id: 'node-w1', label: 'PROD-1042: Token Expiry Bug', type: 'work_item', key: 'jira:PROD-1042', properties: { priority: 'High', status: 'In Progress' } },
      { id: 'node-w2', label: 'RFC-89: Graph Service Spec', type: 'work_item', key: 'notion:doc_982374', properties: { status: 'Under Review' } }
    ],
    edges: [
      { id: 'e-slack', from: 'node-p1', to: 'node-id-slack', type: 'has_identity', confidence: 1.0, status: 'confirmed', method: 'slack_oauth_verified' },
      { id: 'e-github', from: 'node-p1', to: 'node-id-github', type: 'has_identity', confidence: 1.0, status: 'confirmed', method: 'github_oauth_verified' },
      { id: 'e-jira', from: 'node-p1', to: 'node-id-jira', type: 'has_identity', confidence: 0.95, status: 'confirmed', method: 'roster_match' },
      { id: 'e-email', from: 'node-p1', to: 'node-id-email', type: 'has_identity', confidence: 1.0, status: 'confirmed', method: 'google_workspace' },
      { id: 'e3', from: 'node-p1', to: 'node-e1', type: 'attended', confidence: 0.95, status: 'confirmed', method: 'calendar_rsvp' },
      { id: 'e4', from: 'node-p2', to: 'node-e1', type: 'organized', confidence: 1.0, status: 'confirmed', method: 'calendar_creator' },
      { id: 'e5', from: 'node-p1', to: 'node-w1', type: 'assigned_to', confidence: 0.99, status: 'confirmed', method: 'jira_assignee' },
      { id: 'e6', from: 'node-p1', to: 'node-m1', type: 'authored', confidence: 1.0, status: 'confirmed', method: 'slack_user_id' },
      { id: 'e7', from: 'node-m1', to: 'node-w1', type: 'mentions', confidence: 0.88, status: 'candidate', method: 'regex_extract', evidence: { context: 'Discussed in thread' } },
      { id: 'e8', from: 'node-p2', to: 'node-w2', type: 'reviewed', confidence: 0.82, status: 'candidate', method: 'notion_comment', evidence: { context: 'Feedback added' } }
    ]
  };

  let graphInstance = null;

  async function fetchGraph(forceRefresh = false) {
    if (!graphCanvas) return;
    try {
      if (state.actingUserId) {
        let url = `/webhooks/graph?owner_user_id=${encodeURIComponent(state.actingUserId)}`;
        if (forceRefresh) url += '&refresh=true';
        const response = await fetch(url, {
          headers: {
            'X-Agenda-Token': state.sharedToken,
            'X-Acting-User-Secret': state.userSecret
          }
        });
        if (response.ok) {
          const data = await response.json();
          if (data && data.nodes && data.nodes.length > 0) {
            renderGraphReviewQueue(data.edges || [], data.nodes || []);
            const dataJson = JSON.stringify(data);
            if (!forceRefresh && dataJson === state.lastGraphDataJson) return;
            state.lastGraphDataJson = dataJson;
            renderGraphInteractive(data);
            return;
          }
        }
      }
      // Graceful fallback to rich sample graph
      renderGraphReviewQueue(sampleGraphData.edges, sampleGraphData.nodes);
      renderGraphInteractive(sampleGraphData);
    } catch (err) {
      console.warn('fetchGraph fallback active:', err);
      renderGraphReviewQueue(sampleGraphData.edges, sampleGraphData.nodes);
      renderGraphInteractive(sampleGraphData);
    }
  }

  function renderGraphInteractive(data) {
    try {
      const nodes = (data && data.nodes && data.nodes.length) ? data.nodes : sampleGraphData.nodes;
      const edges = (data && data.edges && data.edges.length) ? data.edges : sampleGraphData.edges;

      if (!window.cytoscape) {
        graphCanvas.innerHTML = '<div class="empty-state"><h3>Graph renderer loading</h3><p>Initializing Cytoscape engine…</p></div>';
        return;
      }
      graphCanvas.innerHTML = '';

      const nodeIcons = {
        person: '👤',
        identity: '🔑',
        event: '📅',
        message: '💬',
        work_item: '📋'
      };

      // Filter valid nodes and ensure unique IDs
      const validNodeIds = new Set();
      const cleanNodes = [];
      for (const node of nodes) {
        if (node && node.id && !validNodeIds.has(node.id)) {
          validNodeIds.add(node.id);
          const icon = nodeIcons[node.type] || '📌';
          cleanNodes.push({
            data: {
              id: node.id,
              rawLabel: node.label || node.id,
              label: `${icon}  ${node.label || node.id}`,
              type: node.type || 'unknown',
              key: node.key || '',
              properties: node.properties || {}
            }
          });
        }
      }

      // Filter edges to only include those whose source and target exist in validNodeIds
      const cleanEdges = [];
      const seenEdgeIds = new Set();
      for (const edge of edges) {
        if (edge && edge.id && edge.from && edge.to && validNodeIds.has(edge.from) && validNodeIds.has(edge.to)) {
          if (!seenEdgeIds.has(edge.id)) {
            seenEdgeIds.add(edge.id);
            const confPercent = (Number(edge.confidence || 0) * 100).toFixed(0);
            cleanEdges.push({
              data: {
                id: edge.id,
                source: edge.from,
                target: edge.to,
                typeLabel: edge.type ? edge.type.replace(/_/g, ' ') : 'rel',
                fullLabel: `${edge.type} · ${edge.method || 'link'} · ${confPercent}%`,
                confidence: edge.confidence,
                method: edge.method,
                status: edge.status || 'confirmed',
                evidence: edge.evidence
              }
            });
          }
        }
      }

      const elements = {
        nodes: cleanNodes,
        edges: cleanEdges
      };

      const graph = window.cytoscape({
        container: graphCanvas,
        elements: elements,
        layout: {
          name: 'cose',
          animate: true,
          animationDuration: 600,
          fit: true,
          padding: 50,
          nodeRepulsion: 12000,
          idealEdgeLength: 160,
          edgeElasticity: 100,
          gravity: 0.25,
          numIter: 1000
        },
        style: [
          // Base Node Style
          {
            selector: 'node',
            style: {
              'shape': 'round-rectangle',
              'label': 'data(label)',
              'text-wrap': 'wrap',
              'text-max-width': '160px',
              'font-family': 'Plus Jakarta Sans, system-ui, sans-serif',
              'font-size': '11px',
              'font-weight': '600',
              'color': '#F1F5F9',
              'text-valign': 'center',
              'text-halign': 'center',
              'width': 180,
              'height': 48,
              'background-color': '#101420',
              'border-width': 2,
              'border-color': 'rgba(255, 255, 255, 0.12)'
            }
          },
          // Node Specific Color Themes
          {
            selector: 'node[type = "person"]',
            style: {
              'background-color': '#0B2218',
              'border-color': '#10B981',
              'color': '#6EE7B7'
            }
          },
          {
            selector: 'node[type = "identity"]',
            style: {
              'background-color': '#2A1D06',
              'border-color': '#F59E0B',
              'color': '#FDE68A'
            }
          },
          {
            selector: 'node[type = "event"]',
            style: {
              'background-color': '#131A38',
              'border-color': '#6366F1',
              'color': '#C7D2FE'
            }
          },
          {
            selector: 'node[type = "message"]',
            style: {
              'background-color': '#221136',
              'border-color': '#A855F7',
              'color': '#E9D5FF'
            }
          },
          {
            selector: 'node[type = "work_item"]',
            style: {
              'background-color': '#2D1017',
              'border-color': '#F43F5E',
              'color': '#FECDD3'
            }
          },
          // Base Edge Style
          {
            selector: 'edge',
            style: {
              'curve-style': 'bezier',
              'width': 2,
              'line-color': '#4F46E5',
              'target-arrow-color': '#6366F1',
              'target-arrow-shape': 'triangle',
              'arrow-scale': 1.1,
              'label': 'data(typeLabel)',
              'font-family': 'Plus Jakarta Sans, system-ui, sans-serif',
              'font-size': '9px',
              'font-weight': '600',
              'color': '#94A3B8',
              'text-background-color': '#0B0E17',
              'text-background-opacity': 0.95,
              'text-background-padding': '3px',
              'text-background-shape': 'roundrectangle',
              'text-rotation': 'autorotate'
            }
          },
          // Candidate Review Edges
          {
            selector: 'edge[status = "candidate"]',
            style: {
              'line-color': '#D97706',
              'target-arrow-color': '#F59E0B',
              'line-style': 'dashed',
              'color': '#FDE68A'
            }
          },
          // Dynamic Interactive Classes: Highlighted State
          {
            selector: 'node.highlighted',
            style: {
              'border-width': 3,
              'border-color': '#818CF8',
              'opacity': 1,
              'z-index': 99
            }
          },
          {
            selector: 'edge.highlighted',
            style: {
              'width': 3.5,
              'line-color': '#818CF8',
              'target-arrow-color': '#A5B4FC',
              'color': '#FFFFFF',
              'opacity': 1,
              'z-index': 98
            }
          },
          // Dynamic Interactive Classes: Dimmed State for focus
          {
            selector: 'node.dimmed',
            style: {
              'opacity': 0.2
            }
          },
          {
            selector: 'edge.dimmed',
            style: {
              'opacity': 0.12
            }
          },
          // Selected State
          {
            selector: 'node:selected',
            style: {
              'border-width': 3,
              'border-color': '#FFFFFF',
              'opacity': 1,
              'z-index': 100
            }
          }
        ],
        userZoomingEnabled: true,
        userPanningEnabled: true,
        boxSelectionEnabled: false
      });

      graphInstance = graph;

      // Focus Highlighting: Tap/Hover on node emphasizes connected neighborhood
      function highlightNeighborhood(targetNode) {
        const neighborhood = targetNode.neighborhood().add(targetNode);
        graph.elements().removeClass('highlighted dimmed');
        graph.elements().not(neighborhood).addClass('dimmed');
        neighborhood.addClass('highlighted');
      }

      function clearHighlight() {
        graph.elements().removeClass('highlighted dimmed');
      }

      graph.on('tap', 'node', (event) => {
        const targetNode = event.target;
        highlightNeighborhood(targetNode);
        const nodeData = nodes.find((item) => item.id === targetNode.id());
        showGraphDetails(nodeData, edges, nodes);
      });

      graph.on('tap', (event) => {
        if (event.target === graph) {
          clearHighlight();
        }
      });

      // Hook up zoom/fit controls
      const btnZoomIn = document.getElementById('btn-graph-zoom-in');
      const btnZoomOut = document.getElementById('btn-graph-zoom-out');
      const btnFit = document.getElementById('btn-graph-fit');

      if (btnZoomIn) {
        btnZoomIn.onclick = () => {
          graph.zoom(graph.zoom() * 1.3);
          graph.center();
        };
      }
      if (btnZoomOut) {
        btnZoomOut.onclick = () => {
          graph.zoom(graph.zoom() * 0.7);
          graph.center();
        };
      }
      if (btnFit) {
        btnFit.onclick = () => {
          graph.fit(undefined, 40);
        };
      }
    } catch (err) {
      console.error('renderGraphInteractive error:', err);
      graphCanvas.innerHTML = '<div class="empty-state"><h3>Graph rendering error</h3><p>Could not format graph elements.</p></div>';
    }
  }

  function showGraphDetails(node, edges, nodes) {
    if (!node || !graphDetails) return;
    const related = edges.filter((edge) => edge.from === node.id || edge.to === node.id);
    const candidateCount = related.filter((edge) => edge.status === 'candidate').length;
    const confirmedCount = related.length - candidateCount;

    const nodeIcons = {
      person: '👤',
      identity: '🔑',
      event: '📅',
      message: '💬',
      work_item: '📋'
    };
    const icon = nodeIcons[node.type] || '📌';

    graphDetails.innerHTML = `
      <div style="display:flex; align-items:center; gap:8px;">
        <span>${icon}</span>
        <span class="graph-detail-name">${escapeHtml(node.label)}</span>
        <span class="graph-detail-type">${escapeHtml(node.type.replace('_', ' '))}</span>
      </div>
      <span class="graph-detail-key">${escapeHtml(node.key)}</span>
      ${node.properties?.url ? `<a href="${escapeHtml(node.properties.url)}" target="_blank" rel="noopener noreferrer" class="graph-detail-link">Open source ↗</a>` : ''}
      <span class="graph-detail-meta">${related.length} relationship${related.length === 1 ? '' : 's'} (${confirmedCount} confirmed${candidateCount > 0 ? ` · <strong style="color:var(--warning)">${candidateCount} review</strong>` : ''})</span>
    `;
  }

  // REAL BUG FOUND AND FIXED (confirmed live via a real headless-browser
  // run: fetchGraph's own real /webhooks/graph call succeeded with real
  // node/edge data, but immediately threw "ReferenceError:
  // renderGraphReviewQueue is not defined" — called at the top of BOTH
  // fetchGraph's success path and its own fallback/catch path, so the
  // graph canvas's static "No graph data yet" placeholder was NEVER once
  // replaced, no matter what the API actually returned). The "Needs
  // review" panel (#graph-review-list/#graph-review-count) already
  // existed in the HTML with a working action handler
  // (reviewGraphEdge below) — this was the missing piece that actually
  // populates it from a fetched edge list.
  function renderGraphReviewQueue(edges, nodes) {
    if (!graphReviewList || !graphReviewCount) return;
    const labelById = {};
    for (const node of nodes || []) {
      if (node && node.id) labelById[node.id] = node.label || node.id;
    }
    const candidates = (edges || []).filter((e) => e && e.status === 'candidate');

    graphReviewCount.textContent = String(candidates.length);
    if (candidates.length === 0) {
      graphReviewList.innerHTML = '<div class="empty-state"><p>No candidate relationships.</p></div>';
      return;
    }

    graphReviewList.innerHTML = candidates.map((edge) => {
      const fromLabel = labelById[edge.from] || edge.from || '?';
      const toLabel = labelById[edge.to] || edge.to || '?';
      const typeLabel = edge.type ? String(edge.type).replace(/_/g, ' ') : 'relationship';
      const confPercent = Math.round(Number(edge.confidence || 0) * 100);
      return `
        <div class="agenda-item-card" data-edge-id="${escapeHtml(edge.id)}">
          <div class="item-main-row">
            <div class="item-content">
              <div class="item-text-input" style="cursor: default;">${escapeHtml(fromLabel)} → ${escapeHtml(toLabel)}</div>
              <div class="item-meta-row">
                <span class="badge">${escapeHtml(typeLabel)}</span>
                <span class="badge">${confPercent}% confidence</span>
              </div>
            </div>
            <div class="item-actions">
              <button type="button" class="btn-icon graph-review-confirm" title="Confirm" aria-label="Confirm">✓</button>
              <button type="button" class="btn-icon graph-review-reject" title="Reject" aria-label="Reject">✕</button>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  // Delegated (not per-row) — the list above is fully rebuilt on every
  // fetchGraph(), same reasoning the checklist's own pending-list click
  // handler already gives for this pattern.
  if (graphReviewList) {
    graphReviewList.addEventListener('click', (e) => {
      const row = e.target.closest('[data-edge-id]');
      if (!row) return;
      const edgeId = row.dataset.edgeId;
      if (e.target.closest('.graph-review-confirm')) {
        reviewGraphEdge(edgeId, 'confirmed');
      } else if (e.target.closest('.graph-review-reject')) {
        reviewGraphEdge(edgeId, 'rejected');
      }
    });
  }

  async function reviewGraphEdge(edgeId, decision) {
    const response = await fetch('/webhooks/graph/review', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Agenda-Token': state.sharedToken,
        'X-Acting-User-Secret': state.userSecret
      },
      body: JSON.stringify({
        owner_user_id: state.actingUserId,
        edge_id: edgeId,
        reviewer_id: state.actingUserId,
        decision
      })
    });
    if (response.ok) fetchGraph();
  }

  btnRefreshGraph?.addEventListener('click', () => fetchGraph(true));

  // Filter items according to PairScope visibility rule
  function filterItemsByVisibility(items) {
    return items.filter(item => {
      if (item.visibility === 'shared') return true;
      if (item.visibility === 'report_only') return state.actingRole === 'report';
      if (item.visibility === 'manager_only') return state.actingRole === 'manager';
      return true;
    });
  }

  // Render Full Dashboard State
  function render() {
    renderStaleItems();
    renderAgendaItems();
  }

  // Render Stale Consent Section
  function renderStaleItems() {
    const visibleStale = filterItemsByVisibility(state.staleItems);

    if (visibleStale.length === 0) {
      staleSectionEl.style.display = 'none';
      staleListEl.innerHTML = '';
      return;
    }

    staleSectionEl.style.display = 'block';
    staleListEl.innerHTML = visibleStale.map(item => `
      <div class="agenda-item-card stale-card" data-id="${item.id}">
        <div class="item-main-row">
          <div class="item-content">
            <span class="item-text-display">${escapeHtml(item.text)}</span>
            <div class="item-meta-row" style="margin-top: 8px;">
              <span class="badge badge-source-${item.source}">${formatSource(item.source)}</span>
              <span class="badge badge-vis">${item.visibility}</span>
              <span class="surfaced-pill">Surfaced ${item.surfaced_count || 0}x</span>
            </div>
          </div>
          <div class="item-actions">
            <button class="btn-keep" onclick="handleConsentKeep('${item.id}', this)" aria-label="Keep this item on the agenda">Keep</button>
            <button class="btn-drop" onclick="handleConsentDrop('${item.id}', this)" aria-label="Drop this item">Drop</button>
          </div>
        </div>
      </div>
    `).join('');
  }

  // Render Active Rolling Agenda Items
  function renderAgendaItems() {
    const visibleItems = filterItemsByVisibility(state.agendaItems);
    itemCountPill.textContent = `${visibleItems.length} Item${visibleItems.length === 1 ? '' : 's'}`;

    if (visibleItems.length === 0) {
      agendaListEl.innerHTML = '';
      agendaListEl.appendChild(emptyStateEl);
      emptyStateEl.style.display = 'block';
      return;
    }

    emptyStateEl.style.display = 'none';
    agendaListEl.innerHTML = visibleItems.map(item => `
      <div class="agenda-item-card" data-id="${item.id}">
        <div class="item-main-row">
          <div class="item-content">
            <input
              type="text"
              class="item-text-input"
              value="${escapeHtml(item.text)}"
              aria-label="Edit item text"
              onchange="handleFieldEdit('${item.id}', this.value)"
            />
            <div class="item-meta-row" style="margin-top: 8px;">
              <span class="badge badge-source-${item.source}">${formatSource(item.source)}</span>
              <button
                type="button"
                class="badge badge-vis badge-vis-clickable"
                title="Click to change who can see this"
                onclick="handleVisibilityCycle('${item.id}', '${item.visibility}', this)"
              >${item.visibility}</button>
              ${safeSourceLink(item.source_link) ? `<a href="${escapeHtml(safeSourceLink(item.source_link))}" target="_blank" rel="noopener noreferrer" class="surfaced-pill" style="text-decoration: underline;">Source ↗</a>` : ''}
              <span class="surfaced-pill">Surfaced ${item.surfaced_count || 0}x</span>
            </div>
          </div>
          <div class="item-actions">
            <button class="btn-resolve" onclick="handleResolve('${item.id}', this)" aria-label="Mark this item resolved">✓ Resolve</button>
          </div>
        </div>
      </div>
    `).join('');
  }

  // Quick-Add Manual Note Handler
  async function handleAddNote(e) {
    e.preventDefault();
    const text = noteTextInput.value.trim();
    const visibility = noteVisibilityHidden.value || state.selectedVisibility || 'shared';

    if (!text) return;

    const newItem = {
      id: 'item-' + Date.now(),
      text: text,
      source: 'manual',
      source_link: null,
      visibility: visibility,
      status: 'open',
      surfaced_count: 0,
      created_by_role: state.actingRole
    };

    state.agendaItems.unshift(newItem);
    noteTextInput.value = '';
    charCounter.textContent = '0 / 300';
    charCounter.className = 'char-counter';
    render();
    showToast('Added note to rolling agenda', 'success');
    addLog(`Note added: "${text.slice(0, 40)}${text.length > 40 ? '…' : ''}" [${visibility}]`, 'success');

    // Send API call to backend
    try {
      await fetch('/webhooks/agenda-trigger', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        },
        body: JSON.stringify({
          trigger_type: 'manual_note',
          report_user_id: state.reportUserId,
          acting_user_id: state.actingUserId,
          text: text,
          visibility: visibility
        })
      });
      addLog('API call → /webhooks/agenda-trigger [OK]', 'info');
    } catch (err) {
      addLog('Backend unreachable — local state only', 'error');
    }
  }

  // Post-Meeting Flow Trigger Handler — this is the REAL meeting_end
  // trigger: run_post_meeting_flow's Capture -> Synthesize -> Deliver
  // SequentialAgent (real LLM calls) runs backgrounded server-side
  // (POST returns 202 immediately, per app/triggers/agenda_router.py's
  // non-blocking design). There's no per-stage progress signal from the
  // backend, so the stage animation below is a generic "working on it"
  // indicator, not a literal trace of server-side progress — labeled
  // honestly rather than faked as precise.
  // Tries a real Fathom transcript first (GET /webhooks/fathom-transcript,
  // wired this session); falls back to the dropdown's sample excerpt when
  // Fathom isn't connected (`mentor fathom-auth` never run) or has no
  // matching recording yet — same "real content over nothing" reasoning
  // as the sample-excerpt fallback this replaces on top of.
  async function fetchRealTranscript() {
    try {
      const url = `/webhooks/fathom-transcript?report_user_id=${state.reportUserId}&acting_user_id=${state.actingUserId}`;
      const response = await fetch(url, {
        headers: {
          'X-Agenda-Token': state.sharedToken,
          'X-Acting-User-Secret': state.userSecret
        }
      });
      if (!response.ok) return { transcript: null, status: 'error' };
      const data = await response.json();
      return { transcript: data.transcript_text, status: data.status };
    } catch {
      return { transcript: null, status: 'error' };
    }
  }

  // ── Post-Meeting Flow Controller ────────────────────────────────────
  // Drives Capture -> Map OKRs -> Synthesize -> Deliver with live step-by-step
  // UI operations status, animated pipeline indicators, fast polling for newly
  // generated OKRs/notes/ledgers, and an instant user-abort / Stop button.
  let isMeetingPipelineActive = false;
  let meetingPipelineAborted = false;
  let meetingStepInterval = null;

  const PIPELINE_STAGE_INFOS = [
    { id: 'stage-capture', title: 'Step 1/4: Capturing transcript & raw notes…', btnLabel: 'Capturing…' },
    { id: 'stage-map-okrs', title: 'Step 2/4: Mapping topics to Notion OKRs…', btnLabel: 'Mapping OKRs…' },
    { id: 'stage-synthesize', title: 'Step 3/4: Synthesizing commitments & agenda…', btnLabel: 'Synthesizing…' },
    { id: 'stage-deliver', title: 'Step 4/4: Delivering summary & updating ledgers…', btnLabel: 'Delivering…' }
  ];

  function updatePipelineStageUI(activeIdx) {
    PIPELINE_STAGE_INFOS.forEach((stage, idx) => {
      const el = document.getElementById(stage.id);
      if (!el) return;
      if (idx < activeIdx) {
        el.classList.remove('active');
        el.classList.add('done');
      } else if (idx === activeIdx) {
        el.classList.remove('done');
        el.classList.add('active');
      } else {
        el.classList.remove('active', 'done');
      }
    });

    [1, 2, 3].forEach((num, idx) => {
      const arrow = document.getElementById(`arrow-${num}`);
      if (arrow) arrow.classList.toggle('done', idx < activeIdx);
    });

    if (activeIdx >= 0 && activeIdx < PIPELINE_STAGE_INFOS.length) {
      if (pipelineStatusBox) {
        pipelineStatusBox.classList.remove('success');
        pipelineStatusBox.style.display = 'flex';
      }
      if (pipelineStatusText) pipelineStatusText.textContent = PIPELINE_STAGE_INFOS[activeIdx].title;
      if (meetingBtnText) meetingBtnText.textContent = PIPELINE_STAGE_INFOS[activeIdx].btnLabel;
    }
  }

  function resetPipelineStageUI() {
    PIPELINE_STAGE_INFOS.forEach((stage) => {
      const el = document.getElementById(stage.id);
      if (el) el.classList.remove('active', 'done');
    });
    [1, 2, 3].forEach((num) => {
      const arrow = document.getElementById(`arrow-${num}`);
      if (arrow) arrow.classList.remove('done');
    });
    if (pipelineStatusBox) {
      pipelineStatusBox.classList.remove('success');
      pipelineStatusBox.style.display = 'none';
    }
  }

  function stopMeetingPipeline() {
    if (!isMeetingPipelineActive) return;
    meetingPipelineAborted = true;
    isMeetingPipelineActive = false;
    if (meetingStepInterval) {
      clearInterval(meetingStepInterval);
      meetingStepInterval = null;
    }
    resetPipelineStageUI();
    if (btnStopMeeting) btnStopMeeting.style.display = 'none';
    if (btnRunMeeting) btnRunMeeting.disabled = false;
    if (meetingSpinner) meetingSpinner.style.display = 'none';
    if (meetingBtnText) meetingBtnText.textContent = 'Run post-meeting flow';
    showToast('Post-meeting pipeline stopped.', 'info');
    addLog('Post-meeting pipeline stopped by user', 'info');
  }

  async function handleRunMeeting(e) {
    e.preventDefault();
    const pastedTranscript = meetingTranscriptInput.value.trim();
    const meetingId = `manual-${Date.now()}`;

    meetingPipelineAborted = false;
    isMeetingPipelineActive = true;

    if (btnStopMeeting) btnStopMeeting.style.display = 'inline-flex';
    meetingSpinner.style.display = 'inline-block';
    btnRunMeeting.disabled = true;

    updatePipelineStageUI(0);
    addLog(`Triggering meeting_end for ${meetingId} (real backend call)`, 'signal');

    let currentStageIdx = 0;
    meetingStepInterval = setInterval(() => {
      if (!isMeetingPipelineActive || meetingPipelineAborted) return;
      if (currentStageIdx < 3) {
        currentStageIdx++;
        updatePipelineStageUI(currentStageIdx);
      }
    }, 2800);

    try {
      let transcript = pastedTranscript;
      if (!transcript) {
        const fetched = await fetchRealTranscript();
        if (fetched.status === 'ok' && fetched.transcript) {
          transcript = fetched.transcript;
          addLog('Using a real Fathom transcript for this meeting', 'success');
        } else if (fetched.status === 'not_connected') {
          addLog('Fathom not connected (run `mentor fathom-auth`) — paste a transcript instead', 'info');
        } else if (fetched.status === 'no_transcript_found') {
          addLog('No matching Fathom recording found — paste a transcript instead', 'info');
        }
      }

      if (meetingPipelineAborted) return;

      if (!transcript) {
        showToast('Paste a transcript, or connect Fathom, before running the pipeline.', 'error');
        return;
      }

      // Snapshot initial state across Agenda, Notes, OKRs/Goals, and Ledgers
      // before triggering background execution.
      const initialAgendaCount = state.agendaItems.length;
      const [initialNotes, initialGoals, initialLedgers] = await Promise.all([
        fetchNotes(),
        fetchGoals(),
        fetchLedgers()
      ]);
      const initialNotesCount = (initialNotes || []).length;
      const initialGoalsCount = (initialGoals || []).length;
      const initialCommitmentsCount = (initialLedgers?.commitments || []).length;
      const initialAccomplishmentsCount = (initialLedgers?.accomplishments || []).length;

      if (meetingPipelineAborted) return;

      const { httpStatus } = await postTrigger('meeting_end', {
        meeting_id: meetingId,
        transcript_text: transcript
      });

      if (meetingPipelineAborted) return;

      if (httpStatus === 202) {
        addLog('agenda-trigger accepted (202) — running in the background', 'info');
      }

      const FAST_POLL_BUDGET_MS = 120000;
      const FAST_POLL_INTERVAL_MS = 1200;
      let picked_up = false;
      let terminalStatus = null;
      const deadline = Date.now() + FAST_POLL_BUDGET_MS;

      while (!picked_up && !terminalStatus && Date.now() < deadline) {
        if (meetingPipelineAborted) return;
        await new Promise((r) => setTimeout(r, FAST_POLL_INTERVAL_MS));
        if (meetingPipelineAborted) return;

        const runStatus = await fetchTriggerStatus(meetingId);
        if (runStatus && (runStatus.status === 'error' || runStatus.status === 'rejected')) {
          terminalStatus = runStatus;
          break;
        }

        const [notes, goals, ledgers] = await Promise.all([
          fetchNotes(),
          fetchGoals(),
          fetchLedgers(),
          fetchPayload(true)
        ]);

        const hasNewAgenda = state.agendaItems.length !== initialAgendaCount;
        const hasNewNotes = (notes || []).length !== initialNotesCount;
        const hasNewGoals = (goals || []).length !== initialGoalsCount;
        const hasNewCommitments = (ledgers?.commitments || []).length !== initialCommitmentsCount;
        const hasNewAccomplishments = (ledgers?.accomplishments || []).length !== initialAccomplishmentsCount;
        const isStatusOk = runStatus && runStatus.status === 'ok';

        if (isStatusOk || hasNewAgenda || hasNewNotes || hasNewGoals || hasNewCommitments || hasNewAccomplishments) {
          picked_up = true;
          break;
        }
      }

      if (meetingPipelineAborted) return;

      if (meetingStepInterval) {
        clearInterval(meetingStepInterval);
        meetingStepInterval = null;
      }

      if (picked_up) {
        updatePipelineStageUI(4);
        if (pipelineStatusBox) {
          pipelineStatusBox.classList.add('success');
          pipelineStatusBox.style.display = 'flex';
        }
        if (pipelineStatusText) pipelineStatusText.textContent = '✓ Post-meeting synthesis complete!';
        showToast('Post-meeting synthesis complete! OKRs, notes & ledgers updated.', 'success');
        addLog(`Synthesis complete — new entry/entries logged for ${meetingId}`, 'success');
      } else if (terminalStatus) {
        const reason = terminalStatus.detail || 'unknown error';
        showToast(`Post-meeting synthesis failed: ${reason}`, 'error');
        addLog(`Pipeline ${terminalStatus.status} for ${meetingId}: ${reason}`, 'error');
        if (pipelineStatusText) pipelineStatusText.textContent = `✕ Synthesis failed: ${reason}`;
      } else {
        showToast("Still running in background — updates will reflect automatically.", 'info');
        addLog(`No new entries within ${FAST_POLL_BUDGET_MS / 1000}s — polling continues in background`, 'info');
      }
    } catch (err) {
      if (!meetingPipelineAborted) {
        showToast(`Couldn't trigger meeting synthesis — ${err.message}`, 'error');
        addLog(`Pipeline trigger error: ${err.message}`, 'error');
        if (pipelineStatusText) pipelineStatusText.textContent = `✕ Error: ${err.message}`;
      }
    } finally {
      isMeetingPipelineActive = false;
      if (meetingStepInterval) {
        clearInterval(meetingStepInterval);
        meetingStepInterval = null;
      }
      meetingBtnText.textContent = 'Run post-meeting flow';
      meetingSpinner.style.display = 'none';
      btnRunMeeting.disabled = false;
      if (btnStopMeeting) btnStopMeeting.style.display = 'none';
      if (!meetingPipelineAborted) {
        setTimeout(() => {
          if (!isMeetingPipelineActive) {
            resetPipelineStageUI();
          }
        }, 3500);
      }
    }
  }

  // Signal Simulators
  function simulateSignal(type, text) {
    const item = {
      id: 'sim-' + Date.now(),
      text: text,
      source: type === 'accomplishment' ? 'accomplishment_ledger' : (type === 'jira' ? 'jira' : 'slack'),
      source_link: type === 'jira' ? 'https://jira.internal/browse/JIRA-881' : null,
      visibility: 'shared',
      status: 'open',
      surfaced_count: 0,
      created_by_role: state.actingRole
    };
    state.agendaItems.unshift(item);
    render();
    showToast(`Simulated incoming ${type} signal`, 'success');
    addLog(`Signal ingested [${type.toUpperCase()}]: "${text.slice(0, 50)}…"`, 'signal');
  }

  // Window Scope Handlers for Dynamic Inline HTML Controls — every one
  // of these calls the real backend (via postSignal) and only commits
  // the optimistic local change once the server confirms it; a failure
  // rolls the UI back and surfaces an error toast, rather than silently
  // drifting from what's actually persisted.
  window.handleConsentKeep = async function(itemId, btn) {
    const idx = state.staleItems.findIndex(i => i.id === itemId);
    if (idx === -1) return;
    if (btn) btn.disabled = true;
    try {
      await postSignal('keep', itemId);
      const item = state.staleItems.splice(idx, 1)[0];
      item.status = 'open';
      item.surfaced_count = 0;
      state.agendaItems.unshift(item);
      render();
      showToast('Item kept on agenda. Surfaced count reset to 0.', 'success');
      addLog(`Consent: kept item "${item.text.slice(0, 40)}…"`, 'success');
    } catch (err) {
      showToast(`Couldn't keep item — ${err.message}`, 'error');
      addLog(`Keep failed: ${err.message}`, 'error');
      if (btn) btn.disabled = false;
    }
  };

  window.handleConsentDrop = async function(itemId, btn) {
    const idx = state.staleItems.findIndex(i => i.id === itemId);
    if (idx === -1) return;
    if (btn) btn.disabled = true;
    try {
      await postSignal('drop', itemId);
      const dropped = state.staleItems[idx];
      state.staleItems.splice(idx, 1);
      render();
      showToast('Item dropped from agenda via consent.', 'success');
      addLog(`Consent: dropped item "${dropped.text.slice(0, 40)}…"`, 'info');
    } catch (err) {
      showToast(`Couldn't drop item — ${err.message}`, 'error');
      addLog(`Drop failed: ${err.message}`, 'error');
      if (btn) btn.disabled = false;
    }
  };

  window.handleResolve = async function(itemId, btn) {
    const idx = state.agendaItems.findIndex(i => i.id === itemId);
    if (idx === -1) return;
    if (btn) btn.disabled = true;
    try {
      // mark_resolved's signal_type is "drop" (a resolve and a consent-
      // drop are the same store.py operation — mark_resolved — just
      // triggered from two different parts of this UI).
      await postSignal('drop', itemId);
      const resolved = state.agendaItems[idx];
      state.agendaItems.splice(idx, 1);
      render();
      showToast('Item marked as resolved!', 'success');
      addLog(`Resolved: "${resolved.text.slice(0, 40)}…"`, 'success');
    } catch (err) {
      showToast(`Couldn't resolve item — ${err.message}`, 'error');
      addLog(`Resolve failed: ${err.message}`, 'error');
      if (btn) btn.disabled = false;
    }
  };

  window.handleFieldEdit = async function(itemId, newText) {
    const item = state.agendaItems.find(i => i.id === itemId);
    if (!item) return;
    const previousText = item.text;
    item.text = newText; // optimistic
    try {
      await postSignal('field_edit', itemId, { text: newText });
      showToast('Updated item text', 'success');
      addLog(`Edited item text → "${newText.slice(0, 40)}…"`, 'info');
    } catch (err) {
      item.text = previousText;
      render();
      showToast(`Couldn't save the edit — ${err.message}`, 'error');
      addLog(`Field edit failed: ${err.message}`, 'error');
    }
  };

  const VISIBILITY_CYCLE = ['shared', 'manager_only', 'report_only'];

  window.handleVisibilityCycle = async function(itemId, currentVisibility, btn) {
    const next = VISIBILITY_CYCLE[(VISIBILITY_CYCLE.indexOf(currentVisibility) + 1) % VISIBILITY_CYCLE.length];
    if (btn) btn.disabled = true;
    try {
      await postSignal('field_edit', itemId, { visibility: next });
      const item = state.agendaItems.find(i => i.id === itemId);
      if (item) item.visibility = next;
      render();
      showToast(`Visibility changed to ${next}`, 'success');
      addLog(`Visibility → ${next} for item ${itemId.slice(0, 8)}…`, 'info');
    } catch (err) {
      showToast(`Couldn't change visibility — ${err.message}`, 'error');
      addLog(`Visibility change failed: ${err.message}`, 'error');
      if (btn) btn.disabled = false;
    }
  };

  // Helper Utils
  function formatSource(source) {
    const map = {
      manual: 'Manual note',
      jira: 'Jira blocker',
      slack: 'Slack question',
      accomplishment_ledger: 'Accomplishment',
      commitment_ledger: 'Commitment',
      meeting_synthesis: 'Synthesis'
    };
    return map[source] || source;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // Only http(s) links are ever rendered as a clickable href, and the
  // result is still run through escapeHtml at the call site — source_link
  // is untrusted data (set via ledger_event/jira/slack triggers with no
  // format validation upstream), so a bare "escape the text" pass isn't
  // enough on its own to stop attribute-breakout or javascript: URIs.
  function safeSourceLink(link) {
    if (!link) return null;
    try {
      const url = new URL(link, window.location.origin);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
      return url.href;
    } catch {
      return null;
    }
  }

  function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      setTimeout(() => toast.remove(), 300);
    }, 3000);
  }

  // Start App
  init();
});
