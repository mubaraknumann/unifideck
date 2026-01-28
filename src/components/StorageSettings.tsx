/**
 * Settings Tab Component
 *
 * Contains storage location configuration.
 * Extracted from Content() for tab-based navigation.
 */

import { FC, useState, useEffect } from "react";
import { call, toaster } from "@decky/api";
import {
  PanelSection,
  PanelSectionRow,
  Field,
  Dropdown,
  DropdownOption,
} from "@decky/ui";

import type {
  StorageLocationInfo,
  StorageLocationsResponse,
} from "../types/downloads";

import { t } from "../i18n";

/**
 * Storage Location Settings Component
 */
export const StorageSettings: FC = () => {
  const [locations, setLocations] = useState<StorageLocationInfo[]>([]);
  const [defaultStorage, setDefaultStorage] = useState<string>("internal");
  const [saving, setSaving] = useState(false);

  // Fetch storage locations on mount
  useEffect(() => {
    const fetchLocations = async () => {
      try {
        const result = await call<[], StorageLocationsResponse>(
          "get_storage_locations",
        );
        if (result.success) {
          setLocations(result.locations);
          setDefaultStorage(result.default);
        }
      } catch (error) {
        console.error("[StorageSettings] Error fetching locations:", error);
      }
    };
    fetchLocations();
  }, []);

  // Handle storage location change
  const handleStorageChange = async (option: DropdownOption) => {
    const newLocation = option.data as string;
    setSaving(true);

    try {
      const result = await call<[string], { success: boolean; error?: string }>(
        "set_default_storage_location",
        newLocation,
      );

      if (result.success) {
        setDefaultStorage(newLocation);
        toaster.toast({
          title: t("storageSettings.toastUpdatedTitle"),
          body: t("storageSettings.toastUpdatedBody", {
            location: option.label,
          }),
          duration: 3000,
        });
      } else {
        toaster.toast({
          title: t("storageSettings.toastFailedTitle"),
          body: t("storageSettings.toastFailedBody", {
            error: t(result.error || "Unknown error"),
          }),
          duration: 5000,
          critical: true,
        });
      }
    } catch (error) {
      console.error("[StorageSettings] Error setting storage location:", error);
    }

    setSaving(false);
  };

  // Build dropdown options
  const dropdownOptions: DropdownOption[] = locations
    .filter((loc) => loc.available)
    .map((loc) => ({
      data: loc.id,
      label: t(`${loc.label}`, { freeSpace: `${loc.free_space_gb}` }),
    }));

  const selectedOption = dropdownOptions.find(
    (opt) => opt.data === defaultStorage,
  );

  return (
    <PanelSection title={t("storageSettings.title")}>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "4px",
          width: "100%",
        }}
      >
        <label>{t("storageSettings.installLocationLabel")}</label>
        <div
          style={{ display: "flex", flexDirection: "column", width: "100%" }}
        >
          {dropdownOptions.length > 0 ? (
            <Dropdown
              rgOptions={dropdownOptions}
              selectedOption={selectedOption?.data}
              onChange={handleStorageChange}
              disabled={saving}
            />
          ) : (
            <span style={{ color: "#888", fontSize: "12px" }}>
              {t("storageSettings.loading")}
            </span>
          )}
        </div>
        <p style={{ fontSize: "0.85em", color: "#666" }}>
          {t("storageSettings.installLocationDescription")}
        </p>
      </div>

      {locations.length > 0 && (
        <PanelSectionRow>
          <Field label={t("storageSettings.pathLabel")}>
            <span style={{ color: "#888", fontSize: "12px" }}>
              {locations.find((l) => l.id === defaultStorage)?.path ||
                t("storageSettings.unknown")}
            </span>
          </Field>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
};

export default StorageSettings;
