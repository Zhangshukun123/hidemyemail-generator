((global) => {
  "use strict";

  class TerminalSessionModel {
    constructor({ redactTerminalLogText, inferLogContext, taskStatusMeta }) {
      this.redact = redactTerminalLogText;
      this.inferContext = inferLogContext;
      this.statusMeta = taskStatusMeta;
      this.selectedSessionId = "";
      this.explicitSelection = false;
      this.knownSessionIds = new Set();
      this.lastActiveQuickFlowId = "";
    }

    candidate(kind, label, task, overrides = {}) {
      if (!task || typeof task !== "object") return null;
      const status = String(overrides.status || task.status || (task.running ? "running" : "idle"));
      const running = overrides.running === undefined
        ? Boolean(task.running || task.starting || [
            "queued", "running", "awaiting_otp", "awaiting_captcha", "cancelling",
          ].includes(status))
        : Boolean(overrides.running);
      const currentLogs = Array.isArray(task.logs) ? task.logs : [];
      const historyLogs = Array.isArray(task.historyLogs) ? task.historyLogs : [];
      const logs = Array.isArray(overrides.logs)
        ? overrides.logs
        : running ? currentLogs : historyLogs.length ? historyLogs : currentLogs;
      if (!logs.length && !running && ["", "idle"].includes(status)) return null;
      const id = String(overrides.id || task.processId || task.runId || task.id || task.taskId || "");
      const processId = String(overrides.processId || task.processId || "");
      const startedAt = String(overrides.startedAt || task.startedAt || "");
      const finishedAt = String(overrides.finishedAt || task.finishedAt || "");
      const processLabel = String(overrides.processLabel || task.processLabel || "");
      const email = String(overrides.email || task.email || task.currentEmail ||
        (Array.isArray(task.emails) ? task.emails[0] : "") ||
        logs.find((item) => item && typeof item === "object" && item.email)?.email || "");
      const lastAt = logs.reduce((latest, item) => {
        const at = item && typeof item === "object" ? String(item.at || "") : "";
        return at > latest ? at : latest;
      }, finishedAt || startedAt);
      const sessionId = kind + ":" + (id || processId || processLabel || startedAt || label);
      const displayLabel = processLabel || label + (email ? " · " + email : "");
      return {
        kind, label, task, logs, id, sessionId, processId, processLabel,
        email, startedAt, finishedAt, lastAt, running, status,
        optionLabel: this.redact(displayLabel) + " · " + this.statusMeta(status)[0],
      };
    }

    taskId(task = {}) {
      return String(task.processId || task.id || task.taskId || "");
    }

    taggedLogs(logs, source, extra = {}) {
      return (Array.isArray(logs) ? logs : []).map((raw) => {
        const item = raw && typeof raw === "object" ? { ...raw } : { message: String(raw ?? "") };
        return { source: item.source || source, ...extra, ...item };
      });
    }

    pipelineCandidate(flow, index, managerTasks, claimed) {
      const manager = flow.manager === "protocol" ? "protocol" : "registration";
      const linkedTask = managerTasks.find((task) => this.taskId(task) === String(flow.taskId || ""));
      const linkedId = linkedTask ? this.taskId(linkedTask) : "";
      if (linkedId) claimed.add(manager + ":" + linkedId);
      const paymentLogs = (flow.results || []).flatMap((result) => this.taggedLogs(
        result.paymentLogs,
        "paypal_protocol",
        { originTaskId: result.paymentJobId || "", email: result.email || "" },
      ));
      const linkedLogs = this.taggedLogs(
        linkedTask?.running || !linkedTask?.historyLogs?.length
          ? linkedTask?.logs : linkedTask.historyLogs,
        manager === "protocol" ? "protocol_manager" : "registration_manager",
      );
      const linkedMessages = new Set(linkedLogs.map((item) =>
        String(item.message || "").trim()
      ).filter(Boolean));
      const flowLogs = this.taggedLogs(flow.logs, "quick_flow").filter((item) =>
        !linkedMessages.has(String(item.message || "").trim())
      );
      const logs = [
        ...flowLogs,
        ...linkedLogs,
        ...paymentLogs,
      ];
      const accounts = Array.isArray(linkedTask?.accounts) ? linkedTask.accounts : [];
      const email = String(flow.currentEmail || flow.emails?.[0] || accounts[0]?.email ||
        linkedTask?.email || linkedTask?.currentEmail || "");
      const running = flow.status === "running" || Boolean(linkedTask?.running);
      const status = running ? "running" : String(flow.status || linkedTask?.status || "idle");
      const sequence = String(index + 1).padStart(2, "0");
      return this.candidate("pipeline", "注册提链流水线", {
        ...linkedTask,
        ...flow,
        logs,
        running,
        status,
        email,
      }, {
        id: flow.runId || flow.taskId || "pipeline-" + sequence,
        processId: flow.taskId || linkedId,
        processLabel: "流水线 " + sequence + (email ? " · " + email : ""),
        logs,
        running,
        status,
      });
    }

    phoneBindingCandidate(task, index) {
      const email = String(task.email || "");
      const status = String(task.status || "pending").toLowerCase();
      const logs = this.taggedLogs(task.logs, "phone_binding", {
        email,
        sourceLabel: "手机号绑定",
      });
      return this.candidate("phone-binding", "手机号绑定", {
        ...task,
        logs,
        running: status === "running",
      }, {
        id: task.jobId || email || "phone-binding-" + (index + 1),
        processId: task.jobId || "",
        processLabel: "手机号绑定" + (email ? " · " + email : ""),
        email,
        status,
        running: status === "running",
        startedAt: task.startedAt || logs[0]?.at || "",
        finishedAt: task.finishedAt || "",
        logs,
      });
    }

    managerCandidates(kind, label, parent, tasks, claimed) {
      if (tasks.length) {
        return tasks.map((task, index) => {
          const id = this.taskId(task);
          if (claimed.has(kind + ":" + id)) return null;
          return this.candidate(kind, label, task, {
            id,
            processId: id,
            processLabel: task.processLabel || label + " " + (index + 1),
          });
        }).filter(Boolean);
      }
      const id = this.taskId(parent);
      if (id && claimed.has(kind + ":" + id)) return [];
      const candidate = this.candidate(kind, label, parent, { id, processId: id });
      return candidate ? [candidate] : [];
    }

    buildSessions(state) {
      const registration = state.registrationTask || {};
      const protocol = state.protocolRegistrationTask || {};
      const registrationTasks = Array.isArray(registration.tasks) ? registration.tasks : [];
      const protocolTasks = Array.isArray(protocol.tasks) ? protocol.tasks : [];
      const quickFlows = Array.isArray(state.quickFlows) && state.quickFlows.length
        ? state.quickFlows : state.quickFlow?.runId ? [state.quickFlow] : [];
      const claimed = new Set();
      const sessions = quickFlows.map((flow, index) => this.pipelineCandidate(
        flow,
        index,
        flow.manager === "protocol" ? protocolTasks : registrationTasks,
        claimed,
      )).filter(Boolean);
      const phoneBindings = Array.isArray(state.phoneBindingTasks)
        ? state.phoneBindingTasks : [];
      sessions.push(...phoneBindings.map((task, index) =>
        this.phoneBindingCandidate(task, index)
      ).filter(Boolean));
      sessions.push(...this.managerCandidates(
        "registration", "注册进程", registration, registrationTasks, claimed,
      ));
      sessions.push(...this.managerCandidates(
        "protocol", "Mail Auth 协议注册", protocol, protocolTasks, claimed,
      ));
      [
        this.candidate("browser", "浏览器任务", state.browserTask || {}),
        this.candidate("verification", "账号验证", state.verificationTask || {}),
      ].forEach((session) => { if (session) sessions.push(session); });

      const diagnosticLogs = (registration.failureRecords || []).map((record, index) => ({
        at: record.recordedAt || record.finishedAt || record.startedAt || "",
        email: record.email || (record.emails || [])[0] || "",
        message: "[" + (record.category || "未分类失败") + "] " +
          (record.failureReason || record.message || "注册失败"),
        stage: record.failedStage || record.currentStage || "failed",
        location: record.currentLocation || "注册失败诊断",
        action: record.suggestedAction || "查看任务日志中的失败上下文后重新注册",
        status: "error",
        diagnosticCode: record.reasonCode || "",
        source: "registration_diagnostic",
        eventType: "failure_record",
        taskId: record.taskId || record.processId || registration.id || "",
        sequence: record.sequence || index + 1,
      })).sort((left, right) => new Date(left.at || 0) - new Date(right.at || 0));
      if (diagnosticLogs.length) {
        const diagnostic = this.candidate("registration_diagnostic", "注册诊断", {
          id: registration.id || "registration-diagnostic",
          status: "failed",
          startedAt: diagnosticLogs[0]?.at || "",
          finishedAt: diagnosticLogs.at(-1)?.at || "",
          logs: diagnosticLogs,
        });
        if (diagnostic) sessions.push(diagnostic);
      }
      return sessions.sort((left, right) => {
        if (left.running !== right.running) return left.running ? -1 : 1;
        return new Date(right.lastAt || 0) - new Date(left.lastAt || 0);
      });
    }

    resolveSelection(sessions, state) {
      const activeFlowId = String(state.activeQuickFlowId || "");
      const activeFlowSession = sessions.find((session) =>
        session.kind === "pipeline" && session.id === activeFlowId
      );
      if (activeFlowSession && activeFlowId !== this.lastActiveQuickFlowId) {
        this.selectedSessionId = activeFlowSession.sessionId;
        this.explicitSelection = false;
      }
      this.lastActiveQuickFlowId = activeFlowId;
      const available = new Set(sessions.map((session) => session.sessionId));
      if (!available.has(this.selectedSessionId)) {
        this.selectedSessionId = "";
        this.explicitSelection = false;
      }
      const newRunning = sessions.filter((session) =>
        session.running && !this.knownSessionIds.has(session.sessionId)
      );
      if (!this.explicitSelection && newRunning.length && !activeFlowSession) {
        this.selectedSessionId = newRunning[0].sessionId;
      }
      if (!this.selectedSessionId) {
        this.selectedSessionId = (sessions.find((session) => session.running) || sessions[0])?.sessionId || "";
      }
      this.knownSessionIds = available;
      return sessions.find((session) => session.sessionId === this.selectedSessionId) || null;
    }

    normalizeLogs(session) {
      if (!session) return [];
      const seen = new Set();
      const logs = [];
      session.logs.forEach((raw, index) => {
        const item = raw && typeof raw === "object" ? raw : { message: String(raw ?? "") };
        const message = this.redact(item.message).slice(0, 5000);
        const at = this.redact(item.at || session.startedAt || "");
        const email = String(item.email || session.email || "");
        const taskId = this.redact(item.originTaskId || item.taskId || session.id || "");
        const originSequence = Number(item.originSeq || item.originSequence ||
          (item.originTaskId && item.originTaskId === item.taskId
            ? item.sequence || item.seq : 0));
        const sequence = Number(originSequence || item.sequence || item.seq || index + 1);
        const rawStatus = String(item.level || item.status || "").toLowerCase();
        const normalizedStatus = {
          debug: "active", info: "active", warn: "warning", fatal: "error",
        }[rawStatus] || rawStatus;
        const stableKey = item.originTaskId && originSequence
          ? "origin|" + item.originTaskId + "|" + originSequence
          : [session.sessionId, item.source, item.eventType || item.event_type,
              sequence, at, email, message].join("|");
        if (seen.has(stableKey)) return;
        seen.add(stableKey);
        const contextual = this.inferContext({
          at, email, message,
          stage: this.redact(item.stage),
          location: this.redact(item.location),
          action: this.redact(item.action),
          status: normalizedStatus,
        });
        logs.push({
          ...contextual,
          key: stableKey,
          taskId,
          sequence: Number.isFinite(sequence) && sequence > 0 ? sequence : index + 1,
          source: this.redact(item.source || session.kind),
          eventType: this.redact(item.eventType || item.event_type || "log"),
          sourceLabel: this.redact(item.sourceLabel || session.label),
          processId: this.redact(item.processId || session.processId || ""),
          processLabel: this.redact(item.processLabel || session.processLabel || ""),
          diagnosticCode: this.redact(item.diagnosticCode || item.reasonCode || ""),
        });
      });
      logs.sort((left, right) => {
        const timeDelta = new Date(left.at || 0) - new Date(right.at || 0);
        return (Number.isFinite(timeDelta) ? timeDelta : 0) ||
          Number(left.sequence || 0) - Number(right.sequence || 0);
      });
      return logs.slice(-1200);
    }

    build(state) {
      const sessions = this.buildSessions(state);
      const selected = this.resolveSelection(sessions, state);
      const logs = this.normalizeLogs(selected);
      const runningCount = sessions.filter((session) => session.running).length;
      return {
        logs,
        sessions: sessions.map((session) => ({
          id: session.sessionId,
          label: session.optionLabel,
          running: session.running,
          status: session.status,
        })),
        selectedSessionId: selected?.sessionId || "",
        cursor: logs.at(-1)?.key || "",
        runningCount,
        statusLabel: selected
          ? (runningCount ? runningCount + " 个会话运行中" : "当前无运行任务")
          : "等待任务",
        taskLabel: selected?.optionLabel || "—",
        startedAt: selected?.startedAt || "",
        updatedAt: new Date().toISOString(),
        subtitle: selected ? "当前仅显示“" + selected.optionLabel + "”的日志" : "等待任务启动",
      };
    }

    select(sessionId) {
      this.selectedSessionId = String(sessionId || "");
      this.explicitSelection = true;
    }
  }

  class TerminalLogView {
    constructor({ lookup, escapeHtml, formatLogTimestamp }) {
      this.lookup = lookup;
      this.escapeHtml = escapeHtml;
      this.formatLogTimestamp = formatLogTimestamp;
      this.task = lookup("terminalPreviewTask");
      this.sessionSelect = lookup("terminalSessionSelect");
      this.list = lookup("terminalPreviewList");
      this.scrollFrame = 0;
      this.renderedSessionId = "";
    }

    render(model) {
      const escape = this.escapeHtml;
      const logs = model?.logs || [];
      const sessions = model?.sessions || [];
      const levelLabels = {
        error: "ERROR", warning: "WARN", waiting: "WAIT",
        success: "INFO", active: "INFO", idle: "INFO",
      };
      const sessionChanged = this.renderedSessionId !== model?.selectedSessionId;
      const followTail = sessionChanged ||
        this.list.scrollHeight - this.list.clientHeight - this.list.scrollTop <= 24;
      const previousScrollTop = this.list.scrollTop;
      this.renderedSessionId = model?.selectedSessionId || "";
      this.task.textContent = model?.statusLabel || "等待任务";
      this.sessionSelect.disabled = sessions.length === 0;
      this.sessionSelect.innerHTML = sessions.length ? sessions.map((session) =>
        '<option value="' + escape(session.id) + '">' + escape(session.label) + "</option>"
      ).join("") : '<option value="">暂无日志会话</option>';
      this.sessionSelect.value = model?.selectedSessionId || "";
      this.sessionSelect.title = model?.taskLabel || "暂无日志会话";
      this.list.dataset.sessionId = model?.selectedSessionId || "";
      this.list.innerHTML = logs.length ? logs.map((item) =>
        '<div class="terminal-preview-row" data-level="' + escape(item.status || "idle") +
        '"><time>' + escape(this.formatLogTimestamp(item.at)) + '</time><span class="terminal-preview-level">[' +
        escape(levelLabels[item.status] || "INFO") + ']</span><span class="terminal-preview-source">[' +
        escape(item.sourceLabel || item.source || "WORKSPACE") + ']</span><p>' +
        escape(item.message || "（无消息内容）") + "</p></div>"
      ).join("") : '<div class="terminal-preview-empty">当前日志会话尚无输出。</div>';
      cancelAnimationFrame(this.scrollFrame);
      this.scrollFrame = requestAnimationFrame(() => {
        this.scrollFrame = 0;
        this.list.scrollTop = followTail ? this.list.scrollHeight : previousScrollTop;
      });
    }
  }

  class TerminalLogPresenter {
    constructor(view, model) {
      this.view = view;
      this.model = model;
      this.state = {};
      this.phoneBindings = new Map();
      global.addEventListener("hme:phone-binding-snapshot", (event) => {
        this.ingestPhoneBinding(event.detail || {});
      });
    }

    present(state) {
      this.state = {
        ...(state || {}),
        phoneBindingTasks: [...this.phoneBindings.values()],
      };
      this.view.render(this.model.build(this.state));
    }

    ingestPhoneBinding(detail) {
      const email = String(detail.email || detail.snapshot?.email || "").trim().toLowerCase();
      const snapshot = detail.snapshot && typeof detail.snapshot === "object"
        ? detail.snapshot : {};
      if (!email || !Object.keys(snapshot).length) return;
      const previous = this.phoneBindings.get(email) || { logs: [] };
      const keyed = new Map();
      [...(previous.logs || []), ...(snapshot.logs || [])].forEach((log) => {
        const sequence = Number(log?.sequence || 0);
        if (sequence > 0) keyed.set(sequence, log);
      });
      this.phoneBindings.set(email, {
        ...previous,
        ...snapshot,
        email,
        logs: [...keyed.values()].sort(
          (left, right) => Number(left.sequence || 0) - Number(right.sequence || 0),
        ).slice(-200),
      });
      this.present(this.state);
    }

    select(sessionId) {
      this.model.select(sessionId);
      this.present(this.state);
    }
  }

  global.HmeTerminalLog = {
    TerminalSessionModel,
    TerminalLogView,
    TerminalLogPresenter,
    create(dependencies) {
      return new TerminalLogPresenter(
        new TerminalLogView(dependencies),
        new TerminalSessionModel(dependencies),
      );
    },
  };
})(window);
