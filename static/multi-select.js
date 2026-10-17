/* Shared Shift-click and drag-marquee selection for Inventory and Vending. */
(() => {
  'use strict';

  function bind(options) {
    const checkboxSelector = options.checkboxSelector;
    const isActive = options.isActive;
    const onChange = options.onChange || (() => {});
    const itemSelector = options.itemSelector || '';
    const excludedSelector = options.excludedSelector || 'input,button,a,select,textarea,label';
    const itemClass = options.itemClass || 'selection-drag-target';
    let anchorIndex = null;
    let drag = null;

    const boxes = () => [...document.querySelectorAll(checkboxSelector)];
    function refresh() {
      if (itemSelector) {
        document.querySelectorAll(itemSelector).forEach(item => {
          const checkbox = item.querySelector(checkboxSelector);
          item.classList.toggle('multi-selected', !!checkbox && checkbox.checked);
        });
      }
    }
    function notify() {
      refresh();
      onChange();
    }
    function selectionForClick(box, event) {
      const current = boxes();
      const index = current.indexOf(box);
      if (event.shiftKey && anchorIndex !== null && index >= 0) {
        const start = Math.min(anchorIndex, index);
        const end = Math.max(anchorIndex, index);
        const selected = box.checked;
        for (let i = start; i <= end; i++) current[i].checked = selected;
      }
      anchorIndex = index;
    }

    boxes().forEach(box => {
      box.addEventListener('click', event => {
        if (!isActive()) {
          event.preventDefault();
          return;
        }
        selectionForClick(box, event);
        notify();
      });
      box.addEventListener('change', () => {
        if (isActive()) notify();
      });
    });

    function removeMarquee() {
      if (drag?.marquee) drag.marquee.remove();
      drag = null;
      document.body.classList.remove('selection-dragging');
      document.querySelectorAll('.' + itemClass).forEach(item => item.classList.remove(itemClass));
    }
    function placeMarquee(x1, y1, x2, y2) {
      if (!drag?.marquee) return;
      const left = Math.min(x1, x2);
      const top = Math.min(y1, y2);
      drag.marquee.style.left = `${left}px`;
      drag.marquee.style.top = `${top}px`;
      drag.marquee.style.width = `${Math.abs(x2 - x1)}px`;
      drag.marquee.style.height = `${Math.abs(y2 - y1)}px`;
    }
    function applyMarqueeSelection() {
      if (!drag) return;
      const x1 = Math.min(drag.startX, drag.currentX);
      const y1 = Math.min(drag.startY, drag.currentY);
      const x2 = Math.max(drag.startX, drag.currentX);
      const y2 = Math.max(drag.startY, drag.currentY);
      boxes().forEach(box => { box.checked = drag.baseSelection.has(String(box.value)); });
      document.querySelectorAll(itemSelector).forEach(item => {
        const checkbox = item.querySelector(checkboxSelector);
        if (!checkbox) return;
        const rect = item.getBoundingClientRect();
        const intersects = rect.right >= x1 && rect.left <= x2 && rect.bottom >= y1 && rect.top <= y2;
        if (intersects) checkbox.checked = true;
        item.classList.toggle(itemClass, intersects);
      });
      notify();
    }

    if (itemSelector) {
      document.addEventListener('pointerdown', event => {
        if (!isActive() || event.button !== 0) return;
        const item = event.target.closest(itemSelector);
        if (!item || event.target.closest(excludedSelector)) return;
        const checkbox = item.querySelector(checkboxSelector);
        if (!checkbox) return;
        const additive = event.ctrlKey || event.metaKey || event.shiftKey;
        const priorSelection = new Set(boxes().filter(box => box.checked).map(box => String(box.value)));
        const baseSelection = additive ? priorSelection : new Set();
        if (!additive) {
          boxes().forEach(box => { box.checked = false; });
          notify();
        }
        drag = {
          startX: event.clientX,
          startY: event.clientY,
          currentX: event.clientX,
          currentY: event.clientY,
          moved: false,
          additive,
          ctrlToggle: event.ctrlKey || event.metaKey,
          shift: event.shiftKey,
          targetBox: checkbox,
          targetIndex: boxes().indexOf(checkbox),
          priorSelection,
          baseSelection,
          marquee: document.createElement('div'),
        };
        drag.marquee.className = 'selection-marquee';
        document.body.appendChild(drag.marquee);
        placeMarquee(event.clientX, event.clientY, event.clientX, event.clientY);
        document.body.classList.add('selection-dragging');
        event.preventDefault();
      });

      document.addEventListener('pointermove', event => {
        if (!drag) return;
        drag.currentX = event.clientX;
        drag.currentY = event.clientY;
        if (Math.abs(drag.currentX - drag.startX) > 4 || Math.abs(drag.currentY - drag.startY) > 4) drag.moved = true;
        placeMarquee(drag.startX, drag.startY, drag.currentX, drag.currentY);
        if (drag.moved) applyMarqueeSelection();
      });

      const endDrag = () => {
        if (!drag) return;
        if (!drag.moved) {
          const target = drag.targetBox;
          const current = boxes();
          if (drag.shift && anchorIndex !== null && drag.targetIndex >= 0) {
            const start = Math.min(anchorIndex, drag.targetIndex);
            const end = Math.max(anchorIndex, drag.targetIndex);
            current.forEach((box, index) => {
              if (!drag.additive) box.checked = false;
              if (index >= start && index <= end) box.checked = true;
            });
          } else if (drag.ctrlToggle) {
            target.checked = !drag.priorSelection.has(String(target.value));
          } else {
            current.forEach(box => { box.checked = box === target; });
          }
          anchorIndex = drag.targetIndex;
          notify();
        } else {
          anchorIndex = drag.targetIndex;
          notify();
        }
        removeMarquee();
      };
      document.addEventListener('pointerup', endDrag);
      document.addEventListener('pointercancel', endDrag);
    }

    return {
      resetAnchor() { anchorIndex = null; },
      refresh,
      clearAnchorAndDrag() {
        anchorIndex = null;
        removeMarquee();
        document.querySelectorAll('.multi-selected').forEach(item => item.classList.remove('multi-selected'));
      },
    };
  }

  window.CardScannerMultiSelect = { bind };
})();
