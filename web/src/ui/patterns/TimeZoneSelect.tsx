"use client";

import { useState } from "react";
import { NativeSelect } from "./NativeSelect";

/**
 * The IANA time zones this browser knows. The list is the browser's, so it follows the platform's own time zone database;
 * it leaves out names such as UTC, so `current` is added when it is not in it: the zone a person already has is always
 * one of the choices, however odd its name.
 */
export function timeZoneOptions(current: string): string[] {
  const known = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  return known.includes(current) ? known : [current, ...known];
}

/** A time zone choice by its IANA name ("Asia/Kolkata"). The brief's time is read in this zone (FRONTEND_UI 3.2 and 3.10). */
export function TimeZoneSelect({ value, onChange, label = "Time zone" }: { value: string; onChange: (zone: string) => void; label?: string }) {
  // Taken once, from the zone the field opens with, so choosing another zone does not make the first one vanish from the list.
  const [zones] = useState(() => timeZoneOptions(value));
  return <NativeSelect label={label} value={value} onChange={onChange} options={zones.map((zone) => ({ value: zone, label: zone }))} />;
}
