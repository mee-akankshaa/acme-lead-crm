#!/usr/bin/env python3
"""Assemble index.html = base.html (shared CSS) + extra CSS + body + script (from parts.html)."""
import os
HERE = os.path.dirname(os.path.abspath(__file__))
lead = open(os.path.join(HERE, "base.html"), encoding="utf-8").read()
head = lead.split("</style>")[0]
parts = open(os.path.join(HERE, "parts.html"), encoding="utf-8").read()
body = parts[parts.index("<!--BODY-->") + len("<!--BODY-->"):parts.index("<!--/BODY-->")]
script = parts[parts.index("<!--SCRIPT-->") + len("<!--SCRIPT-->"):parts.index("<!--/SCRIPT-->")]

extra_css = """
a.btn{text-decoration:none}
.login-wrap{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:var(--bg);z-index:100;padding:20px}
.login-card{background:var(--surface);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow-lg);padding:34px 30px;width:100%;max-width:380px;display:flex;flex-direction:column;gap:7px}
.login-logo{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#2F6BFF,#5A8BFF);display:grid;place-items:center;color:#fff;font-weight:800;font-size:24px;margin-bottom:6px}
.login-card h2{margin:0;font-size:20px;font-weight:800}
.login-card>p{margin:0 0 12px;color:var(--muted);font-size:13px}
.login-card label{font-size:11px;font-weight:700;color:var(--ink-2);text-transform:uppercase;letter-spacing:.4px;margin-top:6px}
.login-card input{border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:11px 13px;font-family:inherit;font-size:14px;color:var(--ink)}
.login-card input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.login-btn{justify-content:center;margin-top:14px;padding:12px}
.login-err{background:var(--bad-soft);color:var(--bad);border-radius:9px;padding:9px 12px;font-size:12.5px;font-weight:600;margin-top:8px}
.mini-call{display:inline-grid;place-items:center;width:32px;height:32px;border-radius:9px;border:1px solid var(--line);color:var(--ink-2);text-decoration:none;vertical-align:middle}
.mini-call svg{width:15px;height:15px}
.mini-call:hover{background:var(--good);color:#fff;border-color:var(--good)}
.mini-call.wa:hover{background:#1FA855;border-color:#1FA855}
.mini-call.del:hover{background:var(--bad);color:#fff;border-color:var(--bad)}
.chip-dd{position:relative;display:inline-block}
.chip-dd .dd-car{margin-left:1px;opacity:.65}
.tab-dd{margin-left:auto}
.tab-dd .tab{display:inline-flex;align-items:center}
.dd-menu{display:none;position:absolute;top:calc(100% + 6px);left:0;background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow-lg);padding:5px;min-width:150px;z-index:20}
.dd-menu.show{display:block}
.dd-item{display:flex;align-items:center;gap:7px;width:100%;text-align:left;border:none;background:none;color:var(--ink);padding:8px 10px;border-radius:7px;font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.dd-item .dt{width:8px;height:8px;border-radius:50%;flex:none}
.dd-item:hover{background:var(--surface-2)}
.dd-item.on{background:var(--accent-soft);color:var(--accent)}
.chip .c{color:var(--muted);font-weight:600;margin-left:5px;font-size:11.5px}
.chip.active .c{color:rgba(255,255,255,.75)}
.dd-item .c{margin-left:auto;padding-left:10px}
.pager{display:flex;align-items:center;gap:10px}
.pager .pg-lbl{font-size:12.5px;color:var(--muted);font-weight:600;white-space:nowrap}
.btn-ghost{background:none}
.btn-ghost:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
.btn-ghost:disabled{opacity:.4;cursor:not-allowed}
.bulk-bar{margin-left:auto;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.bulk-total{font-size:12.5px;color:var(--muted);font-weight:600;white-space:nowrap}
.bulk-total b{color:var(--ink)}
.chip-danger{border:1px solid var(--bad);color:var(--bad);background:none}
.chip-danger:hover{background:var(--bad);color:#fff;border-color:var(--bad)}
.chip-danger svg{width:13px;height:13px}
.la-filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:14px 20px 0}
.la-filters input[type=month]{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:20px;padding:6px 12px;font-family:inherit;font-size:12.5px}
.la-filters input[type=month]:focus{outline:none;border-color:var(--accent)}
.la-stats{display:flex;flex-wrap:wrap;padding:16px 4px 4px;margin:14px 20px 0;border-top:1px solid var(--line-2)}
.la-stat{flex:1;min-width:88px;text-align:center;padding:6px 8px}
.la-stat-v{font-size:20px;font-weight:800}
.la-stat-l{font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.4px;margin-top:2px}
.la-chart-wrap{position:relative;padding:14px 20px 18px}
.la-svg{width:100%;height:220px;display:block}
.la-tooltip{position:absolute;transform:translate(-50%,-115%);background:var(--ink);color:var(--surface);padding:7px 11px;border-radius:8px;font-size:11.5px;font-weight:600;pointer-events:none;white-space:nowrap;box-shadow:var(--shadow-lg);z-index:10}
.la-tooltip b{font-size:13px}
.la-tooltip span{display:block;color:rgba(255,255,255,.7);font-weight:500;margin-top:1px}
.float-count{position:fixed;left:50%;bottom:70px;transform:translateX(-50%);background:var(--ink);color:var(--surface);padding:10px 18px;border-radius:12px;font-size:12.5px;font-weight:600;box-shadow:var(--shadow-lg);z-index:90;display:flex;align-items:center;gap:8px;white-space:nowrap;max-width:calc(100vw - 32px)}
.float-count b{color:#fff;font-weight:800}
.float-count svg{flex:none;opacity:.8}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--ink);color:var(--surface);padding:11px 20px;border-radius:10px;font-size:13px;font-weight:600;box-shadow:var(--shadow-lg);opacity:0;pointer-events:none;transition:.25s;z-index:120}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.req-box{border:1px solid var(--line);background:var(--surface-2);border-radius:12px;padding:15px 17px;margin-bottom:20px}
.req-box-h{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700;margin-bottom:9px;display:flex;align-items:center}
.req-full{font-size:14.5px;line-height:1.65;color:var(--ink);white-space:pre-wrap;word-break:break-word;max-height:260px;overflow-y:auto}
.when{color:var(--ink-2);font-weight:600;white-space:nowrap}
#dateFilter input[type=date]{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:8px;padding:5px 8px;font-family:inherit;font-size:12.5px}
#dateFilter input[type=date]:focus{outline:none;border-color:var(--accent)}
/* keep the row actions reachable however narrow the window is */
@media(min-width:561px){
  table thead th:last-child,table tbody td:last-child{position:sticky;right:0;background:var(--surface);box-shadow:-8px 0 8px -8px rgba(19,28,46,.16);z-index:1}
  table tbody tr:hover td:last-child{background:var(--surface-2)}
  .req{max-width:150px}
  table thead th:nth-child(2),table tbody td:nth-child(2){max-width:200px}
  .contact .row{min-width:0}
  .contact .row .tr{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
}
.mini-call.done{background:var(--surface);cursor:pointer}
.mini-call.done:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.mini-call.done.called-today{background:var(--good);color:#fff;border-color:var(--good)}
.mini-call.done.dead-end{background:var(--accent);color:#fff;border-color:var(--accent)}
.fu-date{font-size:11px;color:var(--muted);margin-top:3px;white-space:nowrap}
.note-badge{display:inline-flex;align-items:center;gap:3px;border:none;background:none;color:var(--accent);font-weight:700;font-size:11.5px;cursor:pointer;padding:0;vertical-align:middle}
.note-badge svg{width:12px;height:12px}
.note-badge:hover{text-decoration:underline}
.note-panel{padding:10px 4px}
.note-line{padding:6px 0;border-bottom:1px solid var(--line-2);font-size:13px;line-height:1.5}
.note-line:last-child{border-bottom:none}
.note-date{display:inline-block;min-width:88px;color:var(--ink-2);font-weight:700;font-size:11.5px;margin-right:8px}
.modal-scrim{position:fixed;inset:0;background:rgba(10,15,25,.5);display:none;align-items:center;justify-content:center;z-index:110;padding:20px}
.modal-scrim.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow-lg);width:100%;max-width:430px;max-height:92vh;overflow-y:auto}
.modal-h{display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid var(--line-2);position:sticky;top:0;background:var(--surface);border-radius:16px 16px 0 0}
.modal-h h3{margin:0;font-size:15.5px;font-weight:800;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-h .x{width:32px;height:32px;border-radius:9px;border:1px solid var(--line);background:none;color:var(--ink-2);display:grid;place-items:center;flex:none}
.modal-h .x:hover{border-color:var(--bad);color:var(--bad)}
.modal-b{padding:18px 20px 20px}
.ml{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--ink-2);margin:14px 0 7px}
.ml:first-child{margin-top:0}
.outcomes{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.oc{border:1px solid var(--line);background:var(--surface-2);color:var(--ink);padding:10px 12px;border-radius:10px;font-size:13px;font-weight:600;text-align:left}
.oc:hover{border-color:var(--accent)}
.oc.on{background:var(--accent);border-color:var(--accent);color:#fff}
.modal-b textarea{width:100%;border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:11px 12px;font-family:inherit;font-size:13.5px;color:var(--ink);min-height:76px;resize:vertical}
.modal-b input[type=date],.modal-b #pdAdvance,.modal-b #payAmt,.modal-b input[type=text],.modal-b input[type=tel],.modal-b input[type=email],.modal-b select{width:100%;border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:10px 12px;font-family:inherit;font-size:13.5px;color:var(--ink)}
.modal-b textarea:focus,.modal-b input:focus,.modal-b select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.pay-pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11.5px;font-weight:600;white-space:nowrap}
.pp-paid{background:#e3f6ea;color:#14753b}.pp-partial{background:#fff3d6;color:#8a5a00}.pp-unpaid{background:#fde4e4;color:#b42318}.pp-unknown{background:#e9eaee;color:#555b66}
.pay-list{margin:6px 0 2px;border-top:1px solid var(--line)}
.pay-row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--line);font-size:13.5px}
.pay-row .pay-t{font-size:11.5px;color:var(--muted);margin-left:4px}.pay-row .pay-s{font-size:12px;color:var(--muted);margin-top:2px}
.pay-b{display:flex;gap:2px;flex-shrink:0}
.modal-f{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}
.form-2col{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-2col .ml{margin-top:0}
.name-wrap{display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap}
.name-wrap h2{margin:0}
.name-edit-btn,.name-save-btn,.name-cancel-btn{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:7px;border:1px solid var(--line);background:none;color:var(--ink-2);cursor:pointer;flex:none}
.name-edit-btn svg,.name-save-btn svg,.name-cancel-btn svg{width:13px;height:13px}
.name-edit-btn:hover{border-color:var(--accent);color:var(--accent)}
.name-save-btn:hover{background:var(--good);color:#fff;border-color:var(--good)}
.name-cancel-btn:hover{background:var(--bad);color:#fff;border-color:var(--bad)}
.name-input{border:1px solid var(--accent);background:var(--surface-2);border-radius:8px;padding:4px 9px;font-family:inherit;font-size:19px;font-weight:800;color:var(--ink);text-align:center;max-width:220px;letter-spacing:-.3px}
.name-input:focus{outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
.admin-team-table{width:100%}
.badge-admin{display:inline-block;background:var(--accent-soft);color:var(--accent-ink);font-size:10px;font-weight:700;padding:1px 7px;border-radius:5px;margin-left:6px;letter-spacing:.2px}
.source-rule-row{display:flex;align-items:center;gap:8px;padding:6px 0;flex-wrap:wrap}
.source-rule-row select{border:1px solid var(--line);background:var(--surface-2);border-radius:8px;padding:7px 10px;font-family:inherit;font-size:13px;color:var(--ink);min-width:160px}
#targetsBox input[type=number]{border:1px solid var(--line);background:var(--surface-2);border-radius:8px;padding:7px 10px;font-family:inherit;font-size:13px;color:var(--ink)}
#targetsBox input[type=number]:focus,.source-rule-row select:focus{outline:none;border-color:var(--accent)}
#reassignTo{width:100%;border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:10px 12px;font-family:inherit;font-size:13.5px;color:var(--ink)}
#reassignTo:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
#reassignScope,#reassignFrom,#reassignToDate,#reassignSingleDate,#reassignScheduleDate{width:100%;border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:9px 11px;font-family:inherit;font-size:13.5px;color:var(--ink)}
#reassignScope:focus,#reassignFrom:focus,#reassignToDate:focus,#reassignSingleDate:focus,#reassignScheduleDate:focus{outline:none;border-color:var(--accent)}
.add-user-grid{display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:10px;align-items:end}
.add-user-grid input{width:100%;border:1px solid var(--line);background:var(--surface-2);border-radius:10px;padding:9px 11px;font-family:inherit;font-size:13.5px;color:var(--ink)}
.add-user-grid input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
@media(max-width:700px){.add-user-grid{grid-template-columns:1fr}}
.ef-row{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:3px 0}
.ef-text{display:flex;align-items:center;gap:6px;color:var(--ink-2);font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ef-input{flex:1;border:1px solid var(--accent);background:var(--surface-2);border-radius:8px;padding:5px 9px;font-family:inherit;font-size:13.5px;color:var(--ink)}
.kpi.kpi-link{padding-top:40px}
.k-go{position:absolute;top:10px;right:10px;display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);background:var(--surface-2);color:var(--ink-2);font-family:inherit;font-weight:700;font-size:11px;border-radius:999px;padding:4px 10px;cursor:pointer;transition:all .15s}
.k-go svg{width:13px;height:13px}
.k-go:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.sr-tools{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap}
.sr-year{display:flex;flex-direction:column;gap:4px}
.sr-year label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--ink-2)}
.sr-year select{border:1px solid var(--line);background:var(--surface);border-radius:9px;padding:8px 12px;font-family:inherit;font-size:14px;font-weight:700;color:var(--ink);min-width:96px;cursor:pointer}
.sr-export{display:flex;gap:8px;flex-wrap:wrap}
.sr-note{color:var(--muted);font-size:12.5px;margin:0 0 14px}
.sr-empty{padding:26px 20px;display:flex;flex-direction:column;gap:4px;margin-bottom:16px}
.sr-empty b{font-size:15px}.sr-empty span{color:var(--muted);font-size:13px}
.sr-warn{background:var(--warn-soft,#FFF4DB);color:var(--warn,#9A6200);border-radius:10px;padding:10px 14px;font-size:12.5px;font-weight:600;margin:0 0 16px}
.sr-card{margin-bottom:18px;overflow:hidden}
.sr-card-head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;padding:16px 18px;border-bottom:1px solid var(--line)}
.sr-who{display:flex;align-items:center;gap:12px}
.sr-who b{display:block;font-size:15px}.sr-who span:not(.sr-av){display:block;color:var(--muted);font-size:12px;margin-top:2px}
.sr-av{width:36px;height:36px;border-radius:10px;background:var(--accent-soft);color:var(--accent-ink);display:grid;place-items:center;font-weight:800}
.sr-yt{display:flex;gap:26px;flex-wrap:wrap}
.sr-yt b{display:block;font-size:17px;font-weight:800;font-variant-numeric:tabular-nums}
.sr-yt span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;margin-top:2px}
.sr-table{width:100%;min-width:560px;border-collapse:collapse}
.sr-table th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted);padding:10px 18px;border-bottom:1px solid var(--line);white-space:nowrap}
.sr-table td{padding:11px 18px;border-bottom:1px solid var(--line);font-size:13.5px;vertical-align:top;font-variant-numeric:tabular-nums}
.sr-table tr:last-child td{border-bottom:none}
.sr-click{cursor:pointer;transition:background .12s}
.sr-click:hover{background:var(--accent-soft)}
.sr-go{color:var(--accent);text-align:right;line-height:1}
.sr-go svg{vertical-align:middle}
.sr-ym{table-layout:fixed}
.sr-zero td:not(.sr-go){color:var(--muted)}
.sr-zero td:first-child b{color:var(--ink)}
.sr-group{cursor:pointer;background:var(--surface-2);transition:background .12s}
.sr-group:hover{background:var(--accent-soft)}
.sr-group:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.sr-group td{padding:10px 18px;border-bottom:1px solid var(--line)}
.sr-gico{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:7px;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);margin-right:10px;vertical-align:middle;transition:transform .15s}
.sr-group.open .sr-gico{transform:rotate(180deg)}
.sr-gtxt{color:var(--muted);font-size:12.5px;margin-left:10px}
.sr-none td{color:var(--muted);background:var(--surface-2)}
.sr-none td:first-child{font-weight:600}
.mk-custom span{display:flex;gap:6px}.mk-custom input{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:9px;padding:7px 9px;font-family:inherit;font-size:13px}
.mk-table td small{color:var(--muted);font-size:11px;margin-left:3px}
.mk-table td.m-bad{color:var(--bad);font-weight:700}.mk-table td.m-good{color:var(--good);font-weight:700}
.mk-mix{min-width:130px}@media(max-width:560px){.sr-table.mk-table{min-width:0;width:100%}.mk-table tbody{display:block}.mk-mix .mk-bar{width:55%}.mk-table td{font-size:13px}}
.mk-bar{display:flex;height:9px;border-radius:20px;overflow:hidden;background:var(--line-2)}
.mk-bar i,.mk-legend i{display:block}
.b-won{background:#0E9F6E}.b-int{background:var(--accent)}.b-pen{background:#A9B4CB}.b-not{background:var(--warn)}.b-junk{background:var(--bad)}
.mk-legend{display:flex;flex-wrap:wrap;gap:6px 18px;padding:12px 18px;font-size:12px;color:var(--ink-2)}
.mk-legend span{display:inline-flex;align-items:center;gap:6px}.mk-legend i{width:10px;height:10px;border-radius:3px}
.mk-insight{display:flex;flex-wrap:wrap;align-items:center;gap:10px 38px;padding:14px 20px;margin-bottom:18px}
.mk-insight div span{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}
.mk-insight div b{font-size:19px;font-weight:800}.mk-insight div em{display:block;font-style:normal;font-size:11.5px;color:var(--muted)}
.mk-insight small{color:var(--muted);font-size:11.5px;margin-left:auto}
.mk-others{display:flex;flex-wrap:wrap;gap:8px;padding:16px 18px}
.mk-tag{background:var(--surface-2);border:1px solid var(--line);border-radius:20px;padding:5px 12px;font-size:13px}.mk-tag b{margin-left:8px;color:var(--accent)}
.sr-split{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:18px}
.sr-mini{padding:16px 0 4px}
.sr-mini .ml{padding:0 18px}
.sr-t2{min-width:0}
.sr-sales{min-width:760px}
.sr-r{text-align:right!important}
.sr-sub{color:var(--muted);font-size:11.5px;margin-top:2px}
.sr-item{padding:1px 0}.sr-q{color:var(--ink-2);font-weight:600}.sr-p{color:var(--muted)}
.sr-sales tfoot td{border-top:2px solid var(--line);background:var(--surface-2);padding:12px 18px}
.sr-kpis{margin-bottom:16px;grid-template-columns:repeat(3,1fr)}
@media(max-width:700px){.sr-kpis{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.sr-table th,.sr-table td{padding:9px 12px}}
"""

doc = ('<!doctype html><html lang="en" data-theme="light"><head>'
       '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
       + head + extra_css + "</style></head><body>"
       + body + script + "</body></html>")
open(os.path.join(HERE, "index.html"), "w", encoding="utf-8").write(doc)
print("index.html rebuilt:", len(doc), "bytes")
