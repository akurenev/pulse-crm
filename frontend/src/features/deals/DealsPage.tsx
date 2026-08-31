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
import { useCallback, useDeferredValue, useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Avatar } from "../../components/Avatar";
import { Button } from "../../components/Button";
import { SourceBadge } from "../../components/SourceBadge";
import { formatMoney, formatShortDate } from "../../lib/format";
import { ApiError, remoteEnabled } from "../../lib/api";
import { deepLinkEntityId } from "../../lib/deep-links";
import { DealMutationInProgressError, useCrm, useDeferredSelection } from "../../state/crm-store";
import { DealDrawer } from "./DealDrawer";
import { NewDealDialog } from "./NewDealDialog";
import { StageColumn } from "./StageColumn";

export function DealsPage() {
  const {
    currentUser,
    isEmployee,
    deals,
    pipeline,
    pipelines,
    loading,
    error,
    selectedDealId,
    selectedDeal,
    selectedDealMutationPending,
    dealAssignees,
    openDeal,
    selectPipeline,
    moveDeal,
    setNextPurchase,
    setDealContact,
    setDealCompany,
    setDealAssignee,
    setDealTags,
    setDealSearch,
    setDealCustomFields,
    nextCursorByStage,
    nextDealSearchCursor,
    loadedStageIds,
    stageLoadErrorByStage,
    loadingStageId,
    loadingMoreDealSearch,
    loadStageDeals,
    loadMoreDeals,
    loadMoreDealSearch,
    addDeal,
    deleteDeal,
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
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const routedDealId = deepLinkEntityId(searchParams, "deal");
  const accessGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const kanbanScrollHintId = useId();
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  useEffect(() => {
    setDealSearch(deferredSearch);
  }, [deferredSearch, setDealSearch]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      accessGenerationRef.current += 1;
    };
  }, []);

  const clearDealSelection = useCallback((reason: unknown, accessChanged = false) => {
    setNotice(reason instanceof ApiError && reason.status === 404
      ? "Сделка не найдена или больше вам недоступна."
      : accessChanged
        ? "Не удалось подтвердить доступ к сделке. Откройте её снова."
        : "Не удалось открыть сделку из уведомления.");
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("deal");
      return next;
    }, { replace: true });
    selectDeal(null);
  }, [selectDeal, setSearchParams]);

  useEffect(() => {
    if (!routedDealId) {
      if (selectedDealId) selectDeal(null);
      return;
    }
    if (loading || (selectedDealId === routedDealId && selectedDeal)) return;
    let active = true;
    const controller = new AbortController();
    void openDeal(routedDealId, controller.signal).catch((reason: unknown) => {
      if (!active || controller.signal.aborted) return;
      clearDealSelection(reason);
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [clearDealSelection, loading, openDeal, routedDealId, selectDeal, selectedDeal, selectedDealId]);

  useEffect(() => {
    if (!remoteEnabled) return;
    const purgeSelectedDeal = () => {
      accessGenerationRef.current += 1;
      if (!selectedDealId && !routedDealId) return;
      clearDealSelection(new Error("access changed"), true);
    };
    window.addEventListener("pulse:access-changed", purgeSelectedDeal);
    return () => window.removeEventListener("pulse:access-changed", purgeSelectedDeal);
  }, [clearDealSelection, routedDealId, selectedDealId]);

  function handleSelectDeal(dealId: string) {
    const next = new URLSearchParams(searchParams);
    next.set("deal", dealId);
    setSearchParams(next);
    if (!remoteEnabled || !isEmployee) selectDeal(dealId);
  }

  function handleCloseDeal() {
    const next = new URLSearchParams(searchParams);
    next.delete("deal");
    setSearchParams(next, { replace: true });
    selectDeal(null);
  }

  async function handleSelectPipeline(pipelineId: string) {
    const next = new URLSearchParams(searchParams);
    next.delete("deal");
    setSearchParams(next, { replace: true });
    selectDeal(null);
    await selectPipeline(pipelineId);
  }

  const visibleDeals = useMemo(() => {
    return deals.filter((deal) => {
      const matchesSource = sourceFilter === "all" || deal.source === sourceFilter;
      const matchesSearch = remoteEnabled || !deferredSearch
        || `${deal.title} ${deal.subtitle} ${deal.sourceLabel} ${deal.contactName ?? ""} ${deal.companyName ?? ""} ${deal.phone ?? ""} ${deal.email ?? ""} ${deal.tags.join(" ")}`
          .toLocaleLowerCase("ru")
          .includes(deferredSearch);
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
        : reason instanceof DealMutationInProgressError
          ? reason.message
          : reason instanceof ApiError && reason.status === 409
            ? "Сделка изменилась у другого пользователя. Данные обновлены — повторите действие."
            : reason instanceof ApiError && reason.status === 404
              ? "Сделка уже удалена другим пользователем."
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
          <select value={pipeline.id} onChange={(event) => void handleSelectPipeline(event.target.value)}>{pipelines.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select>
        </label>
        <label className="search-control">
          <Search size={18} aria-hidden="true" />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Название, контакт или компания" />
        </label>
        <label className={`select-control deals-source-filter${filtersOpen ? " is-open" : ""}`}>
          <span className="sr-only">Источник сделки</span>
          <select aria-label="Источник сделки" value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}>
            <option value="all">Все источники</option>
            {sourceOptions.map(([code, label]) => <option key={code} value={code}>{label}</option>)}
          </select>
        </label>
        <button type="button" className="mobile-add mobile-fab" onClick={() => setCreateOpen(true)} aria-label="Новая сделка"><Plus size={23} aria-hidden="true" /></button>
        <div className="view-switch mobile-layout-switch" aria-label="Вид сделок на мобильном">
          <button type="button" className={layout === "list" ? "is-active" : ""} aria-label="Список" onClick={() => setLayout("list")}><List size={18} /></button>
          <button type="button" className={layout === "kanban" ? "is-active" : ""} aria-label="Kanban" onClick={() => setLayout("kanban")}><Columns3 size={18} /></button>
        </div>
      </div>

      {notice ? <button className="toast" type="button" onClick={() => setNotice(null)}>{notice}</button> : null}

      {loading ? <div className="route-loading" role="status">Загружаем сделки…</div> : null}
      {error ? <div className="load-error" role="alert">{error}</div> : null}

      {!loading && !error && layout === "kanban" ? <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={(event) => void handleDragEnd(event)}>
        <div
          className="kanban kanban--single-row kanban--mobile-scroll"
          role="region"
          aria-label="Воронка продаж"
          aria-describedby={kanbanScrollHintId}
          tabIndex={0}
        >
          <p id={kanbanScrollHintId} className="sr-only">Прокручивайте воронку по горизонтали, чтобы увидеть остальные этапы.</p>
          {pipeline.stages.map((stage) => (
            <StageColumn
              key={stage.id}
              stage={stage}
              deals={visibleDeals.filter((deal) => deal.stageId === stage.id)}
              selectedDealId={selectedDealId}
              onSelect={handleSelectDeal}
              onAdd={() => setCreateOpen(true)}
              deferred={!deferredSearch && !loadedStageIds[stage.id] && (stage.stageType === "won" || stage.stageType === "lost")}
              loadError={stageLoadErrorByStage[stage.id]}
              hasMore={!deferredSearch && Boolean(nextCursorByStage[stage.id])}
              loadingMore={loadingStageId === stage.id}
              requestsBusy={Boolean(loadingStageId) || loadingMoreDealSearch}
              onLoadDeferred={() => loadStageDeals(stage.id)}
              onLoadMore={() => loadMoreDeals(stage.id)}
            />
          ))}
        </div>
      </DndContext> : null}

      {!loading && !error && layout === "list" ? <section className="deals-list" role="region" aria-label="Список сделок">
        <ul className="deals-list__cards">
          {visibleDeals.map((deal) => (
            <li key={deal.id}>
              <button
                className="deals-list__row"
                type="button"
                aria-label={`Открыть сделку ${deal.title}. Этап ${pipeline.stages.find((stage) => stage.id === deal.stageId)?.name ?? "Без этапа"}. Сумма ${formatMoney(deal.amount)}. Источник ${deal.sourceLabel}. Срок ${formatShortDate(deal.dueDate)}. Ответственный ${deal.assignee.name}`}
                onClick={() => handleSelectDeal(deal.id)}
              >
                <span className="deals-list__deal" data-label="Сделка">
                  <span className="company-avatar" aria-hidden="true">{deal.title.slice(0, 1)}</span>
                  <span className="deals-list__identity">
                    <strong title={deal.title}>{deal.title}</strong>
                    <small title={deal.tags.length ? `${deal.subtitle} · ${deal.tags.join(" · ")}` : deal.subtitle}>{deal.tags.length ? `${deal.subtitle} · ${deal.tags.join(" · ")}` : deal.subtitle}</small>
                  </span>
                </span>
                <span className="deals-list__stage" data-label="Этап">
                  <small>Этап</small>
                  <strong>{pipeline.stages.find((stage) => stage.id === deal.stageId)?.name ?? "Без этапа"}</strong>
                  <small>{pipeline.name}</small>
                </span>
                <span className="deals-list__amount" data-label="Сумма">
                  <small>Сумма</small>
                  <strong>{formatMoney(deal.amount)}</strong>
                  <small>{deal.currency}</small>
                </span>
                <span className="deals-list__source" data-label="Источник и срок">
                  <small>Источник и срок</small>
                  <SourceBadge source={deal.source} label={deal.sourceLabel} />
                  <small className="deals-list__due-date">до <time dateTime={deal.dueDate}>{formatShortDate(deal.dueDate)}</time></small>
                </span>
                <span className="owner-line deals-list__owner" data-label="Ответственный">
                  <Avatar user={deal.assignee} size="sm" />
                  <span><small>Ответственный</small><strong>{deal.assignee.name}</strong></span>
                </span>
              </button>
            </li>
          ))}
        </ul>
        {!visibleDeals.length ? <p className="empty-copy">Сделки не найдены</p> : null}
      </section> : null}

      {!loading && !error && deferredSearch && nextDealSearchCursor ? <div className="list-pagination deal-search-pagination">
        <Button
          compact
          className="list-pagination__button"
          disabled={loadingMoreDealSearch}
          onClick={() => void loadMoreDealSearch().catch(() => setNotice("Не удалось загрузить остальные результаты поиска."))}
        >{loadingMoreDealSearch ? "Загружаем…" : "Показать ещё результаты"}</Button>
      </div> : null}

      <NewDealDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onSubmit={async (input) => {
          const accessGeneration = accessGenerationRef.current;
          const created = await addDeal(input);
          if (!mountedRef.current || accessGeneration !== accessGenerationRef.current) return;
          handleSelectDeal(created.id);
        }}
      />
      <DealDrawer
        deal={selectedDeal}
        pipeline={pipeline}
        assignees={isEmployee ? [currentUser] : dealAssignees}
        canAccessCompanies={!isEmployee}
        canDelete={!isEmployee}
        canManageAssignee={!isEmployee}
        mutationPending={selectedDealMutationPending}
        onClose={handleCloseDeal}
        onMove={moveDeal}
        onSetNextPurchase={setNextPurchase}
        onSetContact={setDealContact}
        onSetCompany={setDealCompany}
        onSetAssignee={setDealAssignee}
        onSetTags={setDealTags}
        onSetCustomFields={setDealCustomFields}
        onSendMessage={sendMessage}
        onRetryMessage={retryMessage}
        onToggleTask={toggleTask}
        onDelete={deleteDeal}
        onOpenContact={(contactId) => navigate(`/contacts?contact=${encodeURIComponent(contactId)}`)}
        onOpenCompany={(companyId) => navigate(`/contacts?company=${encodeURIComponent(companyId)}`)}
      />
    </div>
  );
}
