// 留档清理：留档 → 校验 → 清理原始日值。每步都先确认结果再刷新页面。
// 清理是不可逆动作，按钮二次确认；留档与校验幂等，可重复点击。
const state = { busy: false };

function notice(message, isError = false) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

const ACTION_LABEL = {
  archive: { url: (id) => `/api/batches/${id}/archive`, verb: "POST", confirm: null },
  verify: { url: (id) => `/api/batches/${id}/archive/verify`, verb: "GET", confirm: null },
  cleanup: {
    url: (id) => `/api/batches/${id}/cleanup`, verb: "POST",
    confirm: "清理后库内原始日值将删除，对账仅靠留档凭证。确认清理？",
  },
};

async function call(action, batchId) {
  const spec = ACTION_LABEL[action];
  if (spec.confirm && !window.confirm(spec.confirm)) return;
  const response = await fetch(spec.url(encodeURIComponent(batchId)), {
    method: spec.verb,
    headers: { "Content-Type": "application/json" },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error((payload.error || response.status) + " " + (payload.message || ""));
  }
  return payload;
}

document.querySelectorAll(".retention-button").forEach((button) => {
  button.addEventListener("click", async () => {
    if (state.busy) return;
    state.busy = true;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "处理中";
    try {
      const payload = await call(button.dataset.action, button.dataset.batch);
      if (button.dataset.action === "verify") {
        notice("凭证校验通过：" + payload.files.length + " 份文件 sha256 全部一致");
      } else if (button.dataset.action === "cleanup") {
        notice("已清理原始日值 " + payload.pruned_readings + " 行，凭证保留可对账");
      } else {
        notice(payload.reused ? "留档已存在，原凭证未改动" : "留档完成，凭证已写保护");
      }
      window.setTimeout(() => window.location.reload(), 600);
    } catch (error) {
      notice(String(error.message || error), true);
      button.disabled = false;
      button.textContent = original;
    } finally {
      state.busy = false;
    }
  });
});
