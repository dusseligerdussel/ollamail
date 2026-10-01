/**
 * Minimal external store for items that components register while mounted
 * (keyboard shortcuts, command palette actions). Works with `useSyncExternalStore`.
 */
export interface Registry<T extends { id: string }> {
  /** Adds or replaces the item with the same id. Returns a function that removes it again. */
  register(item: T): () => void;
  subscribe(listener: () => void): () => void;
  /** Items in registration order. The array identity only changes when the items change. */
  getSnapshot(): readonly T[];
}

export function createRegistry<T extends { id: string }>(): Registry<T> {
  const items = new Map<string, T>();
  const listeners = new Set<() => void>();
  let snapshot: readonly T[] = [];

  const emit = () => {
    snapshot = [...items.values()];
    for (const listener of listeners) listener();
  };

  return {
    register(item) {
      items.delete(item.id);
      items.set(item.id, item);
      emit();
      return () => {
        // Only remove the item if it has not been replaced by a newer registration.
        if (items.get(item.id) === item) {
          items.delete(item.id);
          emit();
        }
      };
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => snapshot,
  };
}
