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

import type { StorageLocationInfo, StorageLocationsResponse } from "../types/downloads";

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
                const result = await call<[], StorageLocationsResponse>("get_storage_locations");
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
                newLocation
            );

            if (result.success) {
                setDefaultStorage(newLocation);
                toaster.toast({
                    title: "Storage Location Updated",
                    body: `New games will be installed to ${option.label}`,
                    duration: 3000,
                });
            } else {
                toaster.toast({
                    title: "Failed to Update",
                    body: result.error || "Unknown error",
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
            label: `${loc.label} (${loc.free_space_gb} GB free)`,
        }));

    const selectedOption = dropdownOptions.find((opt) => opt.data === defaultStorage);

    return (
        <PanelSection title="DOWNLOAD SETTINGS">
            {dropdownOptions.length > 0 ? (
                <PanelSectionRow>
                    <Dropdown
                        label="Install Location"
                        description="Where new games will be downloaded"
                        rgOptions={dropdownOptions}
                        selectedOption={selectedOption?.data}
                        onChange={handleStorageChange}
                        disabled={saving}
                    />
                </PanelSectionRow>
            ) : (
                <PanelSectionRow>
                    <Field description="Loading storage options..." />
                </PanelSectionRow>
            )}

            {/* Show current default path */}
            {locations.length > 0 && (
                <PanelSectionRow>
                    <Field
                        label="Path"
                        description={locations.find((l) => l.id === defaultStorage)?.path || "Unknown"}
                    />
                </PanelSectionRow>
            )}
        </PanelSection>
    );
};

export default StorageSettings;
