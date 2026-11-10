import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

/** 整屏看板外壳：portal 到 body，避免父级 space-y 的 margin 把 fixed 盒子压小。
 *  `space-y-*` 生成的是 `:where(... > :not(:last-child)) { margin-block-end }`，
 *  作用在 `fixed inset-0` 的元素上会从高度里扣掉该边距，底部露出底层页面。 */
export function FullScreenOverlay({ children }: { children: ReactNode }) {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, []);

  return createPortal(
    <div className="fixed inset-0 z-[60] m-0 flex flex-col bg-background overscroll-contain">
      {children}
    </div>,
    document.body,
  );
}
