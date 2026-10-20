/* Test helper: give a headless compositor a pointer device and keep it until killed.
 *
 * A headless sway has no input devices, so its seat has no pointer and apps never get clicks.
 * On a real desktop the mouse plays this part. Built by tests/test_integration_wayland.py.
 */
#include <stdio.h>
#include <string.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

static struct wl_seat *seat;
static struct zwlr_virtual_pointer_manager_v1 *manager;

static void global(void *data, struct wl_registry *registry, uint32_t name, const char *interface,
                   uint32_t version) {
    if (strcmp(interface, wl_seat_interface.name) == 0)
        seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
    else if (strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name) == 0)
        manager = wl_registry_bind(registry, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
}

static void global_remove(void *data, struct wl_registry *registry, uint32_t name) {}

static const struct wl_registry_listener listener = {global, global_remove};

int main(void) {
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) {
        fprintf(stderr, "cannot connect to the Wayland display\n");
        return 1;
    }
    wl_registry_add_listener(wl_display_get_registry(display), &listener, NULL);
    wl_display_roundtrip(display);
    if (!seat || !manager) {
        fprintf(stderr, "compositor has no wl_seat or wlr virtual pointer\n");
        return 1;
    }
    struct zwlr_virtual_pointer_v1 *pointer = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(manager, seat);
    zwlr_virtual_pointer_v1_motion(pointer, 0, wl_fixed_from_int(0), wl_fixed_from_int(0));
    zwlr_virtual_pointer_v1_frame(pointer);
    wl_display_roundtrip(display);
    puts("ready");
    fflush(stdout);
    while (wl_display_dispatch(display) != -1) {
    }
    return 0;
}
