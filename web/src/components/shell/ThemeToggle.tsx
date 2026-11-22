"use client";

import type { ReactNode } from "react";
import { Button, Menu, MenuItem, MenuTrigger, Popover } from "react-aria-components";
import { useTheme } from "@/lib/theme";
import { parseTheme, type ThemeChoice } from "@/lib/themeScript";
import { CheckIcon, MonitorIcon, MoonIcon, SunIcon } from "../icons/icons";
import menu from "../ui/menu.module.css";

const OPTIONS: { id: ThemeChoice; label: string; icon: ReactNode }[] = [
  { id: "system", label: "System", icon: <MonitorIcon /> },
  { id: "light", label: "Light", icon: <SunIcon /> },
  { id: "dark", label: "Dark", icon: <MoonIcon /> },
];

/** Theme: follow the system, or force light or dark. The choice is remembered in this browser. */
export function ThemeToggle() {
  const { choice, setChoice } = useTheme();
  const current = OPTIONS.find((option) => option.id === choice) ?? OPTIONS[0];
  return (
    <MenuTrigger>
      <Button className={menu.trigger} aria-label={`Theme: ${current.label}`}>
        {current.icon}
      </Button>
      <Popover className={menu.popover} placement="bottom end">
        <Menu
          className={menu.menu}
          aria-label="Theme"
          selectionMode="single"
          disallowEmptySelection
          selectedKeys={[choice]}
          onSelectionChange={(keys) => {
            const [picked] = keys === "all" ? [] : keys;
            if (picked !== undefined) setChoice(parseTheme(String(picked)));
          }}
        >
          {OPTIONS.map((option) => (
            <MenuItem key={option.id} id={option.id} textValue={option.label} className={menu.item}>
              {option.icon}
              <span>{option.label}</span>
              {choice === option.id && (
                <span className={menu.tick}>
                  <CheckIcon />
                </span>
              )}
            </MenuItem>
          ))}
        </Menu>
      </Popover>
    </MenuTrigger>
  );
}
