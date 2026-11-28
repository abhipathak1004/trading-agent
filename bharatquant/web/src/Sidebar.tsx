import { Fragment, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

const DRAWER_QUERY = "(max-width: 1024px)";

export type NavView =
  | "today"
  | "campaign"
  | "opportunities"
  | "trade"
  | "research"
  | "lab"
  | "technical"
  | "reports"
  | "strategy"
  | "agents"
  | "evals"
  | "hedgefund"
  | "data"
  | "schedules"
  | "settings"
  | "guide";

export type NavGroup = {
  label: string;
  items: { view: NavView; label: string; icon: string }[];
};

type Props = {
  groups: NavGroup[];
  view: NavView;
  signedIn: boolean;
  open: boolean;
  onSelect: (view: NavView) => void;
  onClose: () => void;
  toggleRef: RefObject<HTMLButtonElement | null>;
};

export default function Sidebar({
  groups,
  view,
  signedIn,
  open,
  onSelect,
  onClose,
  toggleRef,
}: Props) {
  const asideRef = useRef<HTMLElement>(null);
  const wasOpen = useRef(false);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const [drawerMode, setDrawerMode] = useState(
    () => window.matchMedia(DRAWER_QUERY).matches,
  );

  useEffect(() => {
    const query = window.matchMedia(DRAWER_QUERY);
    const sync = () => setDrawerMode(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (open && !drawerMode) {
      onCloseRef.current();
    }
  }, [open, drawerMode]);

  useEffect(() => {
    if (open && drawerMode) {
      const previousOverflow = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      const frame = requestAnimationFrame(() => {
        asideRef.current
          ?.querySelector<HTMLElement>('[aria-current="page"]')
          ?.focus();
      });
      const onKeyDown = (event: KeyboardEvent) => {
        if (event.key === "Escape") {
          onCloseRef.current();
        }
      };
      window.addEventListener("keydown", onKeyDown);
      wasOpen.current = true;
      return () => {
        cancelAnimationFrame(frame);
        window.removeEventListener("keydown", onKeyDown);
        document.body.style.overflow = previousOverflow;
      };
    }
    if (wasOpen.current) {
      wasOpen.current = false;
      toggleRef.current?.focus();
    }
  }, [open, drawerMode, toggleRef]);

  return (
    <>
      {open && drawerMode && (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-hidden="true"
          tabIndex={-1}
          onClick={onClose}
        />
      )}
      <aside
        ref={asideRef}
        id="app-sidebar"
        className={`sidebar${open ? " sidebar--open" : ""}`}
        aria-label="Primary navigation"
        inert={drawerMode && !open}
      >
        <a className="wordmark" href="/" aria-label="BharatQuant home">
          <span className="wordmark-mark" aria-hidden="true">BQ</span>
          <span>BharatQuant</span>
        </a>

        {signedIn && (
          <nav className="sidebar-nav" aria-label="Workspace">
            {groups.map((group, index) => (
              <Fragment key={group.label}>
                <p
                  className={`nav-label${index > 0 ? " nav-label--secondary" : ""}`}
                >
                  {group.label}
                </p>
                {group.items.map((item) => (
                  <button
                    key={item.view}
                    type="button"
                    className={`nav-item nav-item--button${view === item.view ? " nav-item--active" : ""}`}
                    aria-current={view === item.view ? "page" : undefined}
                    onClick={() => onSelect(item.view)}
                  >
                    <span aria-hidden="true">{item.icon}</span>
                    {item.label}
                  </button>
                ))}
              </Fragment>
            ))}
          </nav>
        )}

        <div className="sidebar-footer">
          <span className="local-dot" aria-hidden="true" />
          <div>
            <strong>Local workspace</strong>
            <span>Loopback only</span>
          </div>
        </div>
      </aside>
    </>
  );
}
