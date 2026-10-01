import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { useListNavigation } from "@/hooks/use-list-navigation";

import { ShortcutProvider, useRegisteredShortcuts, useShortcut } from "./shortcut-provider";

function Counter() {
  const [count, setCount] = useState(0);
  useShortcut({ id: "test.increment", keys: "x", group: "general", description: "Increment" }, () =>
    setCount((value) => value + 1),
  );
  return <output>{count}</output>;
}

function Overview() {
  const shortcuts = useRegisteredShortcuts();
  return (
    <ul>
      {shortcuts.map((shortcut) => (
        <li key={shortcut.id}>{shortcut.description}</li>
      ))}
    </ul>
  );
}

function List({ count }: { count: number }) {
  const { activeIndex } = useListNavigation({ count });
  return <output aria-label="active">{activeIndex}</output>;
}

describe("useShortcut", () => {
  it("registers while mounted and unregisters on unmount", async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <ShortcutProvider>
        <Counter />
        <Overview />
      </ShortcutProvider>,
    );

    await user.keyboard("xx");
    expect(screen.getByRole("status")).toHaveTextContent("2");
    expect(screen.getByRole("listitem")).toHaveTextContent("Increment");

    rerender(
      <ShortcutProvider>
        <Overview />
      </ShortcutProvider>,
    );
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });
});

describe("useListNavigation", () => {
  it("moves through the list with j and k within bounds", async () => {
    const user = userEvent.setup();
    render(
      <ShortcutProvider>
        <List count={3} />
      </ShortcutProvider>,
    );
    const active = screen.getByLabelText("active");
    expect(active).toHaveTextContent("-1");

    await user.keyboard("jjjj");
    expect(active).toHaveTextContent("2");
    await user.keyboard("kkkk");
    expect(active).toHaveTextContent("0");
  });
});
