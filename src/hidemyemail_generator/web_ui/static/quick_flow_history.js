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

  window.HmeQuickFlowHistory = {
    create({ api, store }) {
      return new QuickFlowHistoryPresenter(
        new QuickFlowHistoryModel(api),
        new QuickFlowHistoryView(store),
      );
    },
  };
})();
