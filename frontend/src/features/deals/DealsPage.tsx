import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { sortableKeyboardCoordinates } from "@dnd-kit/sortable";
import { Columns3, Filter, List, Plus, Search } from "lucide-react";
import { useDeferredValue, useMemo, useState } from "react";

import { Avatar } from "../../components/Avatar";
import { Button } from "../../components/Button";
import { SourceBadge } from "../../components/SourceBadge";
import { formatMoney, formatShortDate } from "../../lib/format";
import { ApiError } from "../../lib/api";
import { useCrm, useDeferredSelection } from "../../state/crm-store";
import { DealDrawer } from "./DealDrawer";
import { NewDealDialog } from "./NewDealDialog";
import { StageColumn } from "./StageColumn";

export function DealsPage() {
  const {
    deals,
    pipeline,
    pipelines,
    loading,
    error,
    selectedDealId,
    selectedDeal,
    selectPipeline,
    moveDeal,
    setNextPurchase,
    setDealContact,
    setDealCompany,
    setDealCustomFields,
    nextCursorByStage,
    loadingStageId,
    loadMoreDeals,
    addDeal,
    sendMessage,
    retryMessage,
    toggleTask,
  } = useCrm();
  const selectDeal = useDeferredSelection();
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [layout, setLayout] = useState<"kanban" | "list">("kanban");
  const deferredSearch = useDeferredValue(search.trim().toLocaleLowerCase("ru"));
  const [createOpen, setCreateOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const visibleDeals = useMemo(() => {
    return deals.filter((deal) => {
      const matchesSource = sourceFilter === "all" || deal.source === sourceFilter;
      const matchesSearch = !deferredSearch
        || `${deal.title} ${deal.subtitle} ${deal.sourceLabel}`.toLocaleLowerCase("ru").includes(deferredSearch);
      return matchesSource && matchesSearch;
    });
  }, [deals, deferredSearch, sourceFilter]);

  const sourceOptions = useMemo(() => {
    const options = new Map(deals.map((deal) => [deal.source, deal.sourceLabel]));
    return [...options.entries()].sort((left, right) => left[1].localeCompare(right[1], "ru"));
  }, [deals]);

  async function handleDragEnd(event: DragEndEvent) {
    const dealId = String(event.active.id);
    const stageId = event.over ? String(event.over.id) : null;
    if (!stageId || !pipeline.stages.some((stage) => stage.id === stageId)) return;
    try {
      await moveDeal(dealId, stageId);
      setNotice("Этап сделки обновлён");
    } catch (reason) {
      const details = reason instanceof ApiError ? reason.details as {
        detail?: { code?: string; fields?: Array<{ name?: string; key?: string }> };
      } | undefined : undefined;
      const missing = details?.detail?.code === "missing_required_fields"
        ? details.detail.fields?.map((field) => field.name ?? field.key).filter(Boolean)
        : undefined;
      setNotice(missing?.length
        ? `Заполните обязательные поля: ${missing.join(", ")}`
        : "Не удалось изменить этап. Проверьте обязательные поля.");
    }
  }

  return (
    <div className={`deals-page${selectedDeal ? " deals-page--drawer-open" : ""}`}>
      <header className="page-header deals-toolbar">
        <h1>Сделки</h1>
        <div className="deals-toolbar__actions">
          <div className="view-switch" aria-label="Вид сделок">
            <button type="button" className={layout === "list" ? "is-active" : ""} aria-label="Список" onClick={() => setLayout("list")}><List size={18} /></button>
            <button type="button" className={layout === "kanban" ? "is-active" : ""} aria-label="Kanban" onClick={() => setLayout("kanban")}><Columns3 size={18} /></button>
          </div>
          <Button
            compact
            className={filtersOpen ? "is-active" : ""}
            aria-label="Фильтры"
            aria-expanded={filtersOpen}
            onClick={() => setFiltersOpen((current) => !current)}
          ><Filter size={17} /></Button>
          <Button variant="primary" onClick={() => setCreateOpen(true)}><Plus size={17} /> Новая сделка</Button>
        </div>
      </header>

      <div className="deals-filters">
        <label className="select-control">
          <span className="sr-only">Воронка</span>
          <select value={pipeline.id} onChange={(event) => void selectPipeline(event.target.value)}>{pipelines.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
        </label>
        <label className="search-control">
          <Search size={18} aria-hidden="true" />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Поиск по сделкам" />
        </label>
        <label className={`select-control deals-source-filter${filtersOpen ? " is-open" : ""}`}>
          <span className="sr-only">Источник сделки</span>
          <select aria-label="Источник сделки" value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}>
            <option value="all">Все источники</option>
            {sourceOptions.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
          </select>
        </label>
        <button type="button" className="mobile-add" onClick={() => setCreateOpen(true)} aria-label="Новая сделка"><Plus size={23} /></button>
        <div className="view-switch mobile-layout-switch" aria-label="Вид сделок на мобильном">
          <button type="button" className={layout === "list" ? "is-active" : ""} aria-label="Список" onClick={() => setLayout("list")}><List size={18} /></button>
          <button type="button" className={layout === "kanban" ? "is-active" : ""} aria-label="Kanban" onClick={() => setLayout("kanban")}><Columns3 size={18} /></button>
        </div>
      </div>

      {notice ? <button className="toast" type="button" onClick={() => setNotice(null)}>{notice}</button> : null}

      {loading ? <div className="route-loading" role="status">Загружаем сделки…</div> : null}
      {error ? <div className="load-error" role="alert">{error}</div> : null}

      {!loading && !error && layout === "kanban" ? <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={(event) => void handleDragEnd(event)}>
        <div className="kanban" aria-label="Воронка продаж">
          {pipeline.stages.map((stage) => (
            <StageColumn
              key={stage.id}
              stage={stage}
              deals={visibleDeals.filter((deal) => deal.stageId === stage.id)}
              selectedDealId={selectedDealId}
              onSelect={selectDeal}
              onAdd={() => setCreateOpen(true)}
              hasMore={Boolean(nextCursorByStage[stage.id])}
              loadingMore={loadingStageId === stage.id}
              onLoadMore={() => loadMoreDeals(stage.id)}
            />
          ))}
        </div>
      </DndContext> : null}

      {!loading && !error && layout === "list" ? <section className="data-table deals-list" role="region" aria-label="Список сделок">
        <div className="data-table__header"><span>Сделка</span><span>Этап</span><span>Сумма</span><span>Источник и срок</span><span>Ответственный</span></div>
        {visibleDeals.map((deal) => (
          <button className="data-row" type="button" key={deal.id} onClick={() => selectDeal(deal.id)}>
            <span className="data-row__primary"><span className="company-avatar">{deal.title.slice(0, 1)}</span><span><strong>{deal.title}</strong><small>{deal.subtitle}</small></span></span>
            <span><strong>{pipeline.stages.find((stage) => stage.id === deal.stageId)?.name ?? "Без этапа"}</strong><small>{pipeline.name}</small></span>
            <span><strong>{formatMoney(deal.amount)}</strong><small>{deal.currency}</small></span>
            <span><SourceBadge source={deal.source} label={deal.sourceLabel} /><small>до {formatShortDate(deal.dueDate)}</small></span>
            <span className="owner-line"><Avatar user={deal.assignee} size="sm" /><span>{deal.assignee.name}</span></span>
          </button>
        ))}
      </section> : null}

      <NewDealDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onSubmit={async (input) => { await addDeal(input); }}
      />
      <DealDrawer
        deal={selectedDeal}
        pipeline={pipeline}
        onClose={() => selectDeal(null)}
        onMove={moveDeal}
        onSetNextPurchase={setNextPurchase}
        onSetContact={setDealContact}
        onSetCompany={setDealCompany}
        onSetCustomFields={setDealCustomFields}
        onSendMessage={sendMessage}
        onRetryMessage={retryMessage}
        onToggleTask={toggleTask}
      />
    </div>
  );
}
