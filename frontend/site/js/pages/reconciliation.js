import { api } from '../api.js';
import { can, reconciliationTone, refundTone } from '../config.js';
import {
  badge,
  drawer,
  errorBox,
  errorMessage,
  esc,
  fmtMoney,
  fmtNum,
  fmtTime,
  kv,
  statStrip,
  table,
  toast,
} from '../ui.js';

function statusBanner(summary, role) {
  const pending = summary.pending_run;
  const ok = summary.can_enter_formal;
  return `<div class="recon-banner ${ok ? 'ok' : 'warn'}" data-testid="recon-status">
    <div>
      <span class="eyebrow">退款核对状态</span>
      <div class="recon-status-line">
        ${badge(reconciliationTone(summary.status), summary.status_label)}
        <b>${ok ? '已核对：分群、名单与导出可以使用' : '待核对：正式分群 / 名单生成 / 名单导出已冻结'}</b>
      </div>
      ${
        pending
          ? `<p class="note" style="margin:4px 0 0">
              待核对批次 <span class="mono">${esc(pending.run_no)}</span> ·
              记录 ${fmtNum(pending.record_count)} 条 · 已冲减 ${fmtMoney(pending.refund_amount)} ·
              异常 ${fmtNum(pending.exception_count)} 条 / ${fmtMoney(pending.exception_amount)}
            </p>`
          : '<p class="note" style="margin:4px 0 0">没有待核对批次，最近一次核对已完成落账。</p>'
      }
    </div>
    <div class="recon-actions">
      ${
        pending && can(role, 'refund:reconcile')
          ? `<button class="btn accent" data-testid="confirm-run" data-run="${pending.id}">确认核对并落账</button>`
          : ''
      }
      <button class="btn ghost" data-testid="reload">刷新</button>
    </div>
  </div>`;
}

function checklistTable(checklist) {
  return `<div class="table-wrap"><table class="data recon-table" data-testid="checklist-table">
    <thead><tr><th>核对项</th><th>来源明细</th><th>结论</th></tr></thead>
    <tbody>
      ${checklist.items
        .map(
          (item) => `<tr>
            <td><b>${esc(item.label)}</b><div class="mono muted">${esc(item.key)}</div></td>
            <td>${item.values
              .map(
                (value) => `<div class="recon-value">
                  <span class="mono muted">${esc(value.source)}</span>
                  <span class="mono">${
                    item.key === 'members' || item.key === 'audience_members'
                      ? fmtNum(value.value)
                      : fmtMoney(value.value)
                  }</span>
                </div>`,
              )
              .join('')}</td>
            <td>${item.ok ? badge('ok', '一致') : badge('danger', `差异 ${item.delta}`)}${
              item.note ? `<div class="mini muted">${esc(item.note)}</div>` : ''
            }</td>
          </tr>`,
        )
        .join('')}
    </tbody>
  </table></div>`;
}

function runsTable(runs) {
  return table({
    rows: runs.items,
    columns: [
      { key: 'run_no', label: '批次', className: 'mono' },
      { key: 'batch_key', label: '回传批次号', className: 'mono' },
      { key: 'status', label: '状态', render: (row) => badge(reconciliationTone(row.status), row.status_label) },
      { key: 'record_count', label: '记录', className: 'num', render: (row) => fmtNum(row.record_count) },
      { key: 'applied_count', label: '已冲减', className: 'num', render: (row) => fmtNum(row.applied_count) },
      { key: 'duplicate_count', label: '重复未冲减', className: 'num', render: (row) => fmtNum(row.duplicate_count) },
      {
        key: 'exception_count',
        label: '异常',
        className: 'num',
        render: (row) =>
          row.exception_count
            ? `<b class="danger">${fmtNum(row.exception_count)}</b><div class="mini">${fmtMoney(row.exception_amount)}</div>`
            : '—',
      },
      { key: 'refund_amount', label: '冲减金额', className: 'num', render: (row) => fmtMoney(row.refund_amount) },
      { key: 'confirmed_by', label: '核对人', render: (row) => esc(row.confirmed_by || '—') },
    ],
    empty: { title: '还没有退款批次', hint: '导入一批晚到退款回传后在此核对。' },
  });
}

function recordsTable(records, title) {
  if (!records.length) return '';
  return `<h3 style="margin:14px 0 6px;font-size:0.95rem">${esc(title)}</h3>
    <div class="table-wrap"><table class="data" style="min-width:0">
      <thead><tr><th>退款单号</th><th>订单号</th><th>会员号</th><th>回传时间</th><th>金额</th><th>冲减</th><th>异常额</th><th>处置</th></tr></thead>
      <tbody>${records
        .map(
          (row) => `<tr>
            <td class="mono">${esc(row.refund_no)}</td>
            <td class="mono">${esc(row.order_no)}</td>
            <td class="mono">${esc(row.member_no || '—')}</td>
            <td class="mono">${esc(fmtTime(row.refunded_at))}</td>
            <td class="num">${fmtMoney(row.amount)}</td>
            <td class="num">${fmtMoney(row.applied_amount)}</td>
            <td class="num">${row.exception_amount ? fmtMoney(row.exception_amount) : '—'}</td>
            <td>${badge(refundTone(row.status), row.status_label)}${
              row.exception_reason ? `<div class="mini muted">${esc(row.exception_reason)}</div>` : ''
            }</td>
          </tr>`,
        )
        .join('')}</tbody></table></div>`;
}

function openRun(run, view, ctx) {
  const reconcilable = can(ctx.user.role, 'refund:reconcile');
  const pending = run.status === 'pending';
  const { root, close } = drawer({
    title: `退款核对 ${run.run_no}`,
    subtitle: `${run.batch_key} · ${run.status_label}`,
    body: `${kv([
      ['批次号', `<span class="mono">${esc(run.batch_key)}</span>`],
      ['状态', badge(reconciliationTone(run.status), run.status_label)],
      ['记录数', fmtNum(run.record_count)],
      ['已冲减 / 重复 / 异常', `${fmtNum(run.applied_count)} / ${fmtNum(run.duplicate_count)} / ${fmtNum(run.exception_count)}`],
      ['批次毛额', fmtMoney(run.gross_amount)],
      ['冲减退款', fmtMoney(run.refund_amount)],
      ['净额', fmtMoney(run.net_amount)],
      ['异常金额', fmtMoney(run.exception_amount)],
      ['核对人 / 时间', `${esc(run.confirmed_by || '—')} · ${run.confirmed_at ? esc(fmtTime(run.confirmed_at)) : '—'}`],
      ['备注', esc(run.note || '—')],
    ])}
    ${recordsTable(run.exceptions || [], `无法归属 / 累计超过原单（${run.exception_count} 条）`)}
    ${recordsTable(run.duplicates || [], `重复回传（${run.duplicate_count} 条，未冲减）`)}
    ${recordsTable(
      (run.records || []).filter((row) => row.status === 'applied'),
      `已冲减（${run.applied_count} 条）`,
    )}`,
    footer: `${
      pending && reconcilable
        ? `<button class="btn accent" data-confirm>确认核对并落账</button>
           <span class="note" style="margin-left:8px">${
             run.exception_count ? '存在异常，确认即表示已逐条知晓并列入异常清单' : '无异常，可直接落账'
           }</span>`
        : pending
          ? '<span class="mono muted">只读角色可以查看异常清单，不能确认核对</span>'
          : `<span class="mono muted">该批次已核对，落账后分群与名单已按净额重算</span>`
    }`,
  });
  root.querySelector('[data-confirm]')?.addEventListener('click', async () => {
    try {
      await api.post(`/api/reconciliations/${run.id}/confirm`, {
        acknowledge_exceptions: Boolean(run.exception_count),
      });
      toast('退款核对已落账，分群与待确认名单已按净额重算');
      close();
      await reconciliationPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
    }
  });
}

export const reconciliationPage = {
  async render(view, ctx) {
    let summary;
    let runs;
    let checklist;
    try {
      [summary, runs, checklist] = await Promise.all([
        api.get('/api/reconciliations/summary'),
        api.get('/api/reconciliations', { limit: 20 }),
        api.get('/api/reconciliations/checklist'),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => reconciliationPage.render(view, ctx));
      return;
    }
    const latest = summary.latest_run;
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>退款核对</h2>
          <span class="meta">晚到回传 · 同批重试不重复冲减 · 未核对不进正式分群</span>
        </div>
        <div class="panel-body">${statusBanner(summary, ctx.user.role)}</div>
      </section>

      ${statStrip([
        { label: '数据集毛额', value: fmtMoney(summary.dataset_gross, 0), hint: `窗口 ${summary.window_days} 天` },
        { label: '已核对退款', value: fmtMoney(summary.dataset_refund, 0), hint: '只统计已核对批次' },
        { label: '数据集净额', value: fmtMoney(summary.dataset_net, 0), hint: 'M 分与增量 GMV 口径' },
        {
          label: '最近批次',
          value: latest ? fmtNum(latest.record_count) : '—',
          hint: latest ? `${latest.run_no} · ${latest.status_label}` : '还没有退款批次',
        },
      ])}

      <section class="panel">
        <div class="panel-head">
          <h2>页面 / 分群 / 导出逐项核对</h2>
          <span class="meta">${checklist.all_ok ? '全部一致' : '存在差异'} · ${esc(checklist.status_label)}${
            checklist.export_gated ? ' · 导出已冻结' : ''
          }</span>
        </div>
        <div class="panel-body" data-testid="checklist-wrap">
          <div class="note" style="margin-bottom:8px">
            订单毛额 - 退款额 = 净额；分群人数、名单人数、活动预计 GMV 同源校验。
            ${checklist.export_gated ? '待核对期间导出被冻结，导出列暂不参与比对。' : '导出 CSV 逐行求和与页面一致。'}
          </div>
          ${checklistTable(checklist)}
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h2>退款回传批次</h2>
          <span class="meta">${fmtNum(runs.total)} 个批次 · 点击查看异常与重复明细</span>
        </div>
        <div id="recon-runs">${runsTable(runs)}</div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>核对规则</h2><span class="meta">口径说明</span></div>
        <div class="panel-body note">
          1. 同一回传批次号重复导入只落一次，确认也只冲减一次；<br />
          2. 同一退款单号跨批次再次回传记为「重复回传（未冲减）」，不参与净额；<br />
          3. 订单号不存在、会员号与原单不一致、金额非法 → 无法归属；<br />
          4. 历史已核对退款 + 本笔 &gt; 原单毛额 → 累计超过原单，只冲减剩余可退部分，超出列异常；<br />
          5. 存在「待核对」批次时，重算分群、生成/确认名单、三类导出一律 409 拒绝。
        </div>
      </section>`;

    view.querySelector('[data-testid=reload]')?.addEventListener('click', () => reconciliationPage.render(view, ctx));
    view.querySelector('[data-testid=confirm-run]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const runId = button.dataset.run;
        const detail = await api.get(`/api/reconciliations/${runId}`);
        if (detail.exception_count) {
          button.disabled = false;
          openRun(detail, view, ctx);
          return;
        }
        await api.post(`/api/reconciliations/${runId}/confirm`, { acknowledge_exceptions: false });
        toast('退款核对已落账');
        await reconciliationPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        button.disabled = false;
      }
    });
    view.querySelectorAll('#recon-runs table.data tbody tr').forEach((row) => {
      const item = runs.items.find((entry) => String(entry.id) === row.dataset.id);
      if (item) {
        row.addEventListener('click', async () => {
          try {
            const detail = await api.get(`/api/reconciliations/${item.id}`);
            openRun(detail, view, ctx);
          } catch (err) {
            toast(errorMessage(err), 'err');
          }
        });
      }
    });
  },
};

export default reconciliationPage;
