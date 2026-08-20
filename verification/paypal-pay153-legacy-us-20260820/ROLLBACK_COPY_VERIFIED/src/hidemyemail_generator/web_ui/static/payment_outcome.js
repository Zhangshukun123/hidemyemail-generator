(() => {
  "use strict";

  const PAYMENT_TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
  const CONFIRMATION_TERMINAL_STATUSES = new Set([
    "plus", "not_plus", "refresh_failed", "plus_sms_failed",
  ]);
  const PLUS_CONFIRMED_STATUSES = new Set(["plus", "plus_sms", "plus_sms_failed"]);

  class PaymentOutcomeModel {
    static classify(job = {}) {
      const status = String(job.status || "queued").toLowerCase();
      const result = job.result && typeof job.result === "object" ? job.result : {};
      const confirmation = job.account_confirmation &&
        typeof job.account_confirmation === "object" ? job.account_confirmation : {};
      const confirmationStatus = String(confirmation.status || "").toLowerCase();
      const settlementStatus = String(result.settlement_status || "").toLowerCase();
      const hasProtocolFlag = Object.prototype.hasOwnProperty.call(
        confirmation, "protocol_succeeded",
      );
      const protocolSucceeded = hasProtocolFlag
        ? confirmation.protocol_succeeded === true
        : status === "completed" && result.status === "success" &&
          (!settlementStatus || settlementStatus === "confirmed");
      const protocolTerminal = PAYMENT_TERMINAL_STATUSES.has(status);
      const confirmationFinished = CONFIRMATION_TERMINAL_STATUSES.has(
        confirmationStatus,
      );
      const plusConfirmed = confirmation.plus_confirmed === true || (
        PLUS_CONFIRMED_STATUSES.has(confirmationStatus) &&
        String(confirmation.account_type || "").toLowerCase() === "plus"
      );
      const confirmationFailed = ["not_plus", "refresh_failed"].includes(
        confirmationStatus,
      );
      const deliveryFailed = confirmationStatus === "plus_sms_failed";
      const terminal = protocolTerminal && (!protocolSucceeded || confirmationFinished);
      const detail = String(confirmation.detail || "").trim();
      const paymentError = protocolTerminal && !protocolSucceeded
        ? String(job.error || result.error_code || result.error || "协议支付未成功")
        : "";

      return {
        status,
        result,
        confirmation,
        confirmationStatus,
        protocolSucceeded,
        paymentSucceeded: protocolSucceeded,
        protocolTerminal,
        terminal,
        confirmationFinished,
        confirmationPending: protocolSucceeded && !confirmationFinished,
        plusConfirmed,
        paymentError,
        confirmationError: confirmationFailed
          ? detail || (confirmationStatus === "refresh_failed"
            ? "支付成功，但 AT 刷新失败"
            : "支付成功，但新 AT 未确认 Plus")
          : "",
        deliveryError: deliveryFailed
          ? detail || "支付与 Plus 确认成功，但手机号/Codex 后处理失败"
          : "",
      };
    }
  }

  window.PaymentOutcomeModel = PaymentOutcomeModel;
})();
