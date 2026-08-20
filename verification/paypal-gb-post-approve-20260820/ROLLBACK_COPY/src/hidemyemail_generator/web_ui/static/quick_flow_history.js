(function () {
  "use strict";

  class QuickFlowHistoryModel {
    constructor(api) {
      this.api = api;
      this.queues = new Map();
    }

    async load() {
      const payload = await this.api.get("/api/quick-flow/history");
      return Array.isArray(payload.items) ? payload.items : [];
    }

    enqueue(runId, operation) {
      const key = String(runId || "");
      const previous = this.queues.get(key) || Promise.resolve();
      const queued = previous.catch(() => {}).then(operation);
      this.queues.set(key, queued);
      queued.finally(() => {
        if (this.queues.get(key) === queued) this.queues.delete(key);
      }).catch(() => {});
      return queued;
    }

    save(flow) {
      const snapshot = JSON.parse(JSON.stringify(flow || {}));
      return this.enqueue(snapshot.runId, () =>
        this.api.post("/api/quick-flow/history", { flow: snapshot })
      );
    }

    remove(runId) {
      const target = String(runId || "");
      return this.enqueue(target, () =>
        this.api.post("/api/quick-flow/history/delete", { runId: target })
      );
    }
  }

  class QuickFlowHistoryView {
    constructor(store) {
      this.store = store;
    }

    restore(items) {
      const flows = items.filter((item) => item && item.runId);
      const active = flows.at(-1) || this.store.state.quickFlow;
      this.store.patch({
        quickFlows: flows,
        activeQuickFlowId: active?.runId || "",
        quickFlow: active,
      });
      return flows;
    }
  }

  class QuickFlowHistoryPresenter {
    constructor(model, view) {
      this.model = model;
      this.view = view;
    }

    async restore() {
      return this.view.restore(await this.model.load());
    }

    persist(flow) {
      return this.model.save(flow);
    }

    remove(runId) {
      return this.model.remove(runId);
    }
  }

  class QuickFlowResumePresenter {
    constructor(options) {
      Object.assign(this, options);
    }

    async resume(runId) {
      const flow = this.flowById(runId);
      if (!flow) throw new Error("该中断流水线已不在当前列表中");
      if (flow.status === "running") throw new Error("该流水线已经在运行");
      if (flow.interrupted !== true) throw new Error("只有被中断的流水线可以重新运行");
      const paymentIndex = [...(flow.results || [])].map((item, index) => ({ item, index }))
        .reverse().find(({ item }) => item?.email && item?.url && !item.paymentSucceeded)?.index;
      if (paymentIndex === undefined) {
        const target = String(flow.currentEmail || "").trim().toLowerCase();
        if (flow.phase === "extract" && target) {
          return this.retryCardLink(target, flow.runId);
        }
        throw new Error("该中断阶段没有可恢复的账号或 PayPal 链接");
      }

      const results = (flow.results || []).map((item) => ({ ...item }));
      const result = results[paymentIndex];
      const email = String(result.email || "").trim().toLowerCase();
      const previousJobId = String(result.paymentJobId || "");
      this.patchFlow(flow.runId, {
        status: "running", interrupted: false, phase: "payment", progress: 96,
        postPaymentPhoneBinding: flow.postPaymentPhoneBinding === true,
        currentEmail: email, currentAction: "正在恢复被中断的协议支付",
        message: "正在检查原支付任务并恢复运行", results,
      }, "重新运行中断流水线：" + email +
        (previousJobId ? " · 原任务 " + previousJobId.slice(0, 12) : ""));

      let resumedExistingJob = false;
      if (previousJobId) {
        const model = new window.PayPalPaymentJobModel(result);
        try {
          const payload = await this.api.get(model.endpoint());
          const snapshot = model.apply(payload.job || {});
          Object.assign(result, snapshot.fields || {});
          if (!snapshot.terminal || result.paymentSucceeded) {
            resumedExistingJob = true;
            this.patchFlow(flow.runId, {
              results: [...results], currentAction: snapshot.terminal
                ? "原协议支付任务已完成" : "已连接原协议支付任务，继续监听",
            }, snapshot.terminal
              ? "原协议支付任务已有最终结果"
              : "原协议支付任务仍在运行，已恢复监听");
          }
        } catch (error) {
          if (error.status !== 404) {
            this.patchFlow(flow.runId, {
              status: "failed", interrupted: true, phase: "payment", progress: 99,
              currentAction: "检查原支付任务失败",
              message: "暂时无法确认原支付任务状态，未创建重复任务",
            }, "恢复失败：" + error.message);
            throw error;
          }
        }
      }

      let paymentSucceeded = Boolean(result.paymentSucceeded);
      if (resumedExistingJob && !paymentSucceeded) {
        paymentSucceeded = await this.monitorPayment(flow.runId, result, results);
      } else if (!resumedExistingJob) {
        Object.assign(result, {
          paymentStarted: false, paymentJobId: "", paymentStatus: "", paymentStage: "",
          paymentSucceeded: false, paymentConfirmed: false,
          paymentProtocolSucceeded: false, paymentPlusConfirmed: false,
          paymentPending: false, paymentAtRefreshStatus: "", paymentAtRefreshed: false,
          paymentAccountType: "", paymentError: "", paymentConfirmationError: "",
          paymentDeliveryError: "", paymentPostCheckError: "", paymentLogs: [],
          paymentLogCount: 0, paymentLogSequence: 0,
        });
        const started = await this.startPayment(flow.runId, result, results, 98);
        if (started) {
          paymentSucceeded = await this.monitorPayment(flow.runId, result, results);
        }
      }

      const paymentStarted = results.filter((item) => item.paymentStarted).length;
      const paymentSuccessCount = results.filter((item) => item.paymentSucceeded).length;
      const paymentPlusConfirmed = results.filter((item) => item.paymentPlusConfirmed).length;
      const paymentPending = results.filter((item) => item.paymentPending).length;
      const paymentPostCheckFailed = results.filter((item) => item.paymentPostCheckError).length;
      const failed = results.filter((item) =>
        !item.ok || (item.ok && !item.skipped && !item.paymentSucceeded)
      ).length;
      this.patchFlow(flow.runId, {
        status: paymentSucceeded ? "completed" : "failed",
        interrupted: false,
        phase: paymentSucceeded ? "complete" : "payment",
        progress: 100, results, paymentStarted,
        paymentSucceeded: paymentSuccessCount,
        paymentPlusConfirmed, paymentPending, paymentPostCheckFailed, failed,
        currentEmail: "",
        currentAction: paymentSucceeded
          ? "中断流水线已恢复并完成协议支付"
          : "中断流水线已重新运行，但协议支付失败",
        message: paymentSucceeded
          ? "已从保存的账号和 PayPal 链接恢复；协议支付成功 " + paymentSuccessCount +
            "，新 AT 确认 Plus " + paymentPlusConfirmed
          : "已尝试从中断点恢复；协议支付未成功，可重新提链后再次支付",
      }, paymentSucceeded
        ? "中断流水线重新运行完成：" + email
        : "中断流水线重新运行失败：" + email);
      await this.reloadAccounts();
      return paymentSucceeded
        ? "已恢复并完成协议支付：" + email
        : "已重新运行，但协议支付失败：" + email;
    }
  }

  function runActions(item, escapeHtml) {
    const resumable = item.status === "failed" && item.interrupted === true && (
      (item.phase === "extract" && item.currentEmail) ||
      (item.results || []).some((result) => result?.email && result?.url && !result.paymentSucceeded)
    );
    const runId = escapeHtml(item.runId);
    return '<div class="quick-flow-run-buttons">' + (item.status === "running"
      ? '<button class="button danger small" data-action="stop-quick-flow-run" data-run-id="' +
        runId + '">停止流程</button>'
      : (resumable
          ? '<button class="button primary small" data-action="resume-interrupted-quick-flow" data-run-id="' +
            runId + '">重新运行</button>'
          : '') + '<button class="button small" data-action="dismiss-quick-flow-run" data-run-id="' +
        runId + '">关闭记录</button>') + '</div>';
  }

  window.HmeQuickFlowHistory = {
    create({ api, store }) {
      return new QuickFlowHistoryPresenter(
        new QuickFlowHistoryModel(api),
        new QuickFlowHistoryView(store),
      );
    },
    createResume(options) {
      return new QuickFlowResumePresenter(options);
    },
    runActions,
  };
})();
