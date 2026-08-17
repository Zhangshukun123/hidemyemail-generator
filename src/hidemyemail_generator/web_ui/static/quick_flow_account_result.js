(() => {
  "use strict";

  const normalizedEmail = (value) => String(value || "").trim().toLowerCase();

  class QuickFlowQuotaEligibilityModel {
    static classify(item = {}) {
      const detail = [item.paymentError, item.error].filter(Boolean).join(" · ");
      const codes = [
        item.code,
        item.errorCode,
        item.error_code,
        item.failureCode,
        item.failure_code,
        item.reasonCode,
        item.reason_code,
      ].map((value) => String(value || "").trim().toLowerCase());
      const amountMatch = detail.match(
        /初始金额\s*=\s*(\d+)[\s\S]*?更新后金额\s*=\s*(\d+)/i,
      );
      const initialAmount = amountMatch ? Number(amountMatch[1]) : null;
      const updatedAmount = amountMatch ? Number(amountMatch[2]) : null;
      const unchangedPositiveAmount = Number.isFinite(initialAmount) && initialAmount > 0 &&
        initialAmount === updatedAmount;
      const explicitCode = codes.includes("no_free_quota") || codes.includes("free_trial_ineligible");
      const explicitText = /无免费额度|没有免费额度|不具备免费额度|(?:not|isn't)\s+eligible\s+for\s+(?:the\s+)?(?:free\s+)?trial|free\s+trial\s+(?:is\s+)?(?:unavailable|ineligible)/i.test(detail);
      const unchangedPromotion = /活动更新响应未证明优惠已生效/.test(detail) && unchangedPositiveAmount;
      const noFreeQuota = item.noFreeQuota === true || explicitCode || explicitText || unchangedPromotion;
      return {
        noFreeQuota,
        detail,
        initialAmount,
        updatedAmount,
        reason: noFreeQuota ? "优惠更新前后金额一致，账号没有免费额度" : "",
      };
    }

    static explanation(item = {}) {
      const classification = this.classify(item);
      return classification.noFreeQuota
        ? "账号没有免费额度：优惠更新前后金额一致，已停止重复提链"
        : "";
    }

    static explainFailure(item = {}) {
      const quotaExplanation = this.explanation(item);
      if (quotaExplanation) return quotaExplanation;
      const detail = String(item.paymentError || item.error || "").trim();
      if (!detail) return "未记录到具体错误，请查看下方终端日志";
      if (/chatgpt approve result:\s*['\"]?blocked/i.test(detail)) {
        return "ChatGPT Approve 返回 blocked：本次请求被服务端拦截，不代表账号无法提链；可更换请求或线路后重试";
      }
      if (/邮箱地址无效/.test(detail)) return "主程序未接受该邮箱域名，提链尚未真正发起";
      if (/session\s*\/\s*at 尚未就绪|尚未保存 session\s*\/\s*at/i.test(detail)) {
        return "Session / Access Token 尚未准备完成，提链尚未真正发起";
      }
      return detail;
    }

    removeFromState(state, email, runId) {
      const targetEmail = normalizedEmail(email);
      const targetRunId = String(runId || "");
      const sourceFlows = Array.isArray(state.quickFlows) && state.quickFlows.length
        ? state.quickFlows
        : state.quickFlow?.runId ? [state.quickFlow] : [];
      let changed = false;
      const quickFlows = sourceFlows.map((flow) => {
        if (targetRunId && String(flow.runId || "") !== targetRunId) return flow;
        const previousResults = Array.isArray(flow.results) ? flow.results : [];
        const removed = previousResults.filter((item) => normalizedEmail(item.email) === targetEmail);
        if (!removed.length) return flow;
        changed = true;
        const removedFailures = removed.filter((item) => !item.ok || item.paymentError).length;
        return {
          ...flow,
          results: previousResults.filter((item) => normalizedEmail(item.email) !== targetEmail),
          emails: (flow.emails || []).filter((item) => normalizedEmail(item) !== targetEmail),
          failed: Math.max(0, Number(flow.failed || 0) - removedFailures),
          currentEmail: normalizedEmail(flow.currentEmail) === targetEmail ? "" : flow.currentEmail,
          currentAction: "无免费额度账号已从工作台移除",
          message: "无免费额度账号已从工作台移除",
        };
      });
      if (!changed) return {};
      const activeId = String(state.activeQuickFlowId || targetRunId);
      const active = quickFlows.find((flow) => String(flow.runId || "") === activeId) ||
        quickFlows.at(-1) || state.quickFlow || {};
      return {
        quickFlows,
        quickFlow: active,
        accounts: (state.accounts || []).filter((item) => normalizedEmail(item.email) !== targetEmail),
        selectedAccountEmail: normalizedEmail(state.selectedAccountEmail) === targetEmail
          ? "" : state.selectedAccountEmail,
        selectedCardEmail: normalizedEmail(state.selectedCardEmail) === targetEmail
          ? "" : state.selectedCardEmail,
        selectedVerificationEmail: normalizedEmail(state.selectedVerificationEmail) === targetEmail
          ? "" : state.selectedVerificationEmail,
      };
    }
  }

  class QuickFlowAccountResultView {
    render(model, context) {
      const { item, state, flow, classification } = model;
      const escapeHtml = context.escapeHtml;
      const failureExplanation = context.failureExplanation;
      const failed = !item.ok || Boolean(item.paymentError);
      const postCheckError = String(
        item.paymentPostCheckError || item.paymentConfirmationError ||
        item.paymentDeliveryError || "",
      );
      const visualState = item.ok
        ? (item.paymentError ? "failed" : postCheckError ? "post-check-warning"
          : item.skipped ? "skipped" : "")
        : "failed";
      const statusLabel = item.skipped
        ? (item.skipLabel || "已有同模式链接 · 已跳过")
        : item.ok
          ? (item.paymentError ? "提取链接成功 · 协议支付失败"
            : item.paymentDeliveryError
              ? "提取链接成功 · 支付及 Plus 确认成功 · 后处理失败"
              : item.paymentConfirmationError
                ? "提取链接成功 · 支付成功 · AT/Plus 后置校验失败"
                : item.paymentPlusConfirmed
                  ? "提取链接成功 · 支付成功 · 新 AT 已确认 Plus"
                  : item.paymentPending
                    ? "提取链接成功 · 支付成功 · AT/Plus 确认中"
                    : item.paymentSucceeded ? "提取链接成功 · 支付成功"
                      : item.paymentStarted ? "提取链接成功 · 正在监听协议支付" : "提取链接成功")
          : item.retrying ? "正在重新提链" : "提链未完成 · 可重试";
      const displayedStatus = classification.noFreeQuota ? "提链未完成 · 无免费额度" : statusLabel;
      const detail = [
        item.url || (failed
          ? "失败原因：" + failureExplanation(item) + (item.error ? " · 原始错误：" + item.error : "")
          : item.error || "—"),
        postCheckError ? "支付后状态：" + postCheckError : "",
      ].filter(Boolean).join(" · ");
      const badge = classification.noFreeQuota
        ? '<b class="badge warning quick-flow-quota-badge" title="优惠更新前后金额一致">无免费额度</b>'
        : postCheckError
          ? '<b class="badge warning" title="支付已经成功">后置校验异常</b>'
          : "";
      const paymentAction = item.ok && !item.skipped
        ? context.paymentAction(item, state, flow)
        : "";
      const action = classification.noFreeQuota
        ? '<div class="quick-flow-result-actions"><button class="button danger small" data-action="remove-no-free-quota-account" data-email="' +
          escapeHtml(item.email) + '" data-run-id="' + escapeHtml(flow.runId || "") +
          '" title="仅从本地工作台移除，不删除邮箱服务商侧地址">移除账号</button></div>'
        : item.retryable
          ? '<div class="quick-flow-result-actions"><button class="button small" data-action="retry-quick-card-link" data-email="' +
            escapeHtml(item.email) + '" data-run-id="' + escapeHtml(flow.runId || "") + '"' +
            (flow.status === "running" ? " disabled" : "") + '>重新提链</button></div>'
          : "";
      return '<div class="quick-flow-result ' + visualState +
        (classification.noFreeQuota ? " no-free-quota" : "") + '">' +
        '<div class="quick-flow-result-identity"><strong>' + escapeHtml(item.email) + "</strong>" + badge +
        '</div><span class="quick-flow-result-status">' + displayedStatus + "</span><code>" +
        escapeHtml(detail) + "</code>" + paymentAction + action + "</div>";
    }
  }

  class QuickFlowAccountResultPresenter {
    constructor(api, store, options = {}) {
      this.api = api;
      this.store = store;
      this.model = options.model || new QuickFlowQuotaEligibilityModel();
      this.view = options.view || new QuickFlowAccountResultView();
      this.confirmRemoval = options.confirmRemoval || ((message) => globalThis.confirm(message));
      this.refreshAccounts = options.refreshAccounts || (() => Promise.resolve());
    }

    render(item, state, flow, paymentAction, failureExplanation) {
      return this.view.render({
        item,
        state,
        flow,
        classification: QuickFlowQuotaEligibilityModel.classify(item),
      }, { escapeHtml: this.escapeHtml, paymentAction, failureExplanation });
    }

    escapeHtml(value) {
      return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
    }

    async remove(email, runId) {
      const targetEmail = normalizedEmail(email);
      const flow = (this.store.state.quickFlows || []).find((item) =>
        String(item.runId || "") === String(runId || ""));
      const result = (flow?.results || []).find((item) => normalizedEmail(item.email) === targetEmail);
      if (!result || !QuickFlowQuotaEligibilityModel.classify(result).noFreeQuota) {
        throw new Error("该账号当前未标记为无免费额度");
      }
      const confirmed = this.confirmRemoval(
        "从本地工作台移除 " + targetEmail + "？\n\n该账号已标记为无免费额度；邮箱服务商侧地址不会被删除。",
      );
      if (!confirmed) throw Object.assign(new Error(), { name: "AbortError" });
      const data = await this.api.post("/api/gpt-email/delete", {
        email: targetEmail,
        local_only: true,
      });
      const patch = this.model.removeFromState(this.store.state, targetEmail, runId);
      if (Object.keys(patch).length) this.store.patch(patch);
      await Promise.resolve(this.refreshAccounts()).catch(() => {});
      return "无免费额度账号已移除" + (data.message ? "：" + data.message : "");
    }
  }

  window.QuickFlowQuotaEligibilityModel = QuickFlowQuotaEligibilityModel;
  window.QuickFlowAccountResultPresenter = QuickFlowAccountResultPresenter;
})();
