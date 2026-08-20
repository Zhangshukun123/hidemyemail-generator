(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let currentStatus = { configured: false, source: "" };

  function renderStatus(status) {
    currentStatus = status || currentStatus;
    const badge = $("liandongShopConfigStatus");
    const input = $("liandongShopMerchantToken");
    const save = $("liandongShopSaveToken");
    if (!badge || !input || !save) return;
    const fromEnvironment = currentStatus.source === "environment";
    badge.className = "badge " + (currentStatus.configured ? "success" : "warning");
    badge.textContent = currentStatus.configured
      ? (fromEnvironment ? "环境变量已配置" : "小铺已连接")
      : "等待 Merchant-Token";
    input.placeholder = currentStatus.configured ? "留空保持现有 Token" : "输入 Merchant-Token";
    input.disabled = fromEnvironment;
    save.disabled = fromEnvironment;
  }

  async function refreshStatus(controller) {
    const status = await controller.api.get("/api/liandong-shop/status");
    renderStatus(status);
    return status;
  }

  function register(controller) {
    controller.commands.register("save-liandong-shop-config", async () => {
      const input = $("liandongShopMerchantToken");
      const merchantToken = input.value.trim();
      if (!merchantToken) {
        if (currentStatus.configured) return "联动小铺 Token 未变更";
        throw new Error("请输入联动小铺 Merchant-Token");
      }
      const status = await controller.api.post("/api/liandong-shop/config", { merchantToken });
      input.value = "";
      renderStatus(status);
      return "联动小铺 Token 已保存在本机";
    });
    controller.commands.register("upload-liandong-shop", async ({ element }) => {
      const item = controller.selectedAccount(element.dataset.email);
      if (!item) throw new Error("账号不存在，请刷新后重试");
      if (item.liandongShopUploaded) return "该账号已经上传过，不会重复入库";
      const previousText = element.textContent;
      element.textContent = "上传中…";
      try {
        const data = await controller.api.post("/api/account/liandong-shop-upload", {
          email: item.email,
        });
        await controller.loadAccounts();
        return data.alreadyUploaded
          ? "该账号已经上传过，不会重复入库"
          : "已添加到联动小铺 · " + (data.goodsLabel || data.goodsName);
      } catch (error) {
        element.textContent = previousText;
        throw error;
      }
    });
    refreshStatus(controller).catch((error) => controller.toast(error.message, "error"));
  }

  window.HmeLiandongShop = Object.freeze({ refreshStatus, register, renderStatus });
})();
