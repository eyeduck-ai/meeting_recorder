"""Local Provider testing script for debugging meeting join flow.

This script runs locally (Windows/Linux) with a visible browser window,
making it easy to debug and iterate on Provider login flow.

Two modes:
- Auto mode (default): Runs all steps, outputs debug info on error and exits
- Interactive mode (--interactive): Pauses at each step, user can input ok/error/html/skip

Usage:
    python -m scripts.test_provider --url "https://meet.jit.si/test-room"
    python -m scripts.test_provider --url "https://meet.jit.si/test-room" --interactive
    python -m scripts.test_provider --url "https://meet.jit.si/test-room" --interactive --slowmo 500
"""

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from utils.timezone import utc_now

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from providers import get_provider, list_providers  # noqa: E402


def append_jitsi_muted_config(url: str) -> str:
    """Append Jitsi config params to start with video/audio muted.

    Args:
        url: Original meeting URL

    Returns:
        URL with config params appended (for Jitsi URLs only)
    """
    # Only apply to Jitsi URLs
    if "jit.si" not in url and "jitsi" not in url.lower():
        return url

    # Check if URL already has config params
    if "config.startWithVideoMuted" in url:
        return url

    # Add config params
    separator = "&" if "#config." in url else "#config."
    if "#" not in url:
        separator = "#config."

    config = "startWithVideoMuted=true&config.startWithAudioMuted=true"
    return url + separator + config


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Test Provider meeting join flow locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Auto mode (default) - runs all steps, outputs debug on error
    python -m scripts.test_provider --url "https://meet.jit.si/test-room"

    # Interactive mode - pause at each step
    python -m scripts.test_provider --url "https://meet.jit.si/test-room" --interactive

    # Interactive mode with slow-mo
    python -m scripts.test_provider --url "https://meet.jit.si/test-room" --interactive --slowmo 500

    # Webex test
    python -m scripts.test_provider --url "https://example.webex.com/meet/room" --provider webex
        """,
    )

    parser.add_argument(
        "--url",
        required=True,
        help="Meeting URL to join",
    )
    parser.add_argument(
        "--provider",
        default="jitsi",
        choices=list_providers(),
        help="Provider type (default: jitsi)",
    )
    parser.add_argument(
        "--name",
        default="Test Recorder",
        help="Display name (default: Test Recorder)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Meeting password if required",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Interactive mode: pause at each step, input ok/error/html/skip",
    )
    parser.add_argument(
        "--slowmo",
        type=int,
        default=0,
        help="Slow down actions by N milliseconds",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Join timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--output-dir",
        default="./test_output",
        help="Directory for screenshots (default: ./test_output)",
    )
    parser.add_argument(
        "--no-screenshot",
        action="store_true",
        help="Disable automatic screenshots",
    )

    return parser.parse_args()


def print_step(step: int, message: str) -> None:
    """Print a step message with formatting."""
    print(f"\n{'=' * 60}")
    print(f"  Step {step}: {message}")
    print(f"{'=' * 60}")


def print_info(message: str) -> None:
    """Print an info message."""
    print(f"  [INFO] {message}")


def print_success(message: str) -> None:
    """Print a success message."""
    print(f"  [OK] {message}")


def print_error(message: str) -> None:
    """Print an error message."""
    print(f"  [ERROR] {message}")


async def wait_for_user(message: str = "Press Enter to continue...") -> None:
    """Wait for user input (async-friendly)."""
    print(f"\n  >>> {message}")
    await asyncio.get_event_loop().run_in_executor(None, input)


async def take_screenshot(page, output_dir: Path, name: str) -> Path | None:
    """Take a screenshot and save it."""
    try:
        screenshot_path = output_dir / f"{name}.png"
        await page.screenshot(path=str(screenshot_path))
        print_info(f"Screenshot saved: {screenshot_path}")
        return screenshot_path
    except Exception as e:
        print_error(f"Failed to take screenshot: {e}")
        return None


async def dump_debug_info(page, output_dir: Path, step_name: str, error_msg: str = None) -> None:
    """Dump debug info (screenshot + HTML + iframe HTML + summary) for debugging."""
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{timestamp}_{step_name}"

    print_info("Dumping debug info...")

    # 1. Screenshot
    try:
        screenshot_path = output_dir / f"{prefix}.png"
        await page.screenshot(path=str(screenshot_path))
        print_info(f"Screenshot: {screenshot_path.name}")
    except Exception as e:
        print_error(f"Failed to take screenshot: {e}")

    # 2. Outer HTML content
    try:
        html_path = output_dir / f"{prefix}.html"
        html_content = await page.content()
        html_path.write_text(html_content, encoding="utf-8")
        print_info(f"HTML: {html_path.name}")
    except Exception as e:
        print_error(f"Failed to save HTML: {e}")

    # 3. iframe HTML content (for Webex and other iframe-based providers)
    iframe_html_path = None
    try:
        # Method 1: Try to get Webex unified iframe by URL pattern
        frame = page.frame(name=None, url=lambda url: "web.webex.com" in url)
        if frame:
            iframe_html = await frame.content()
            iframe_html_path = output_dir / f"{prefix}_iframe.html"
            iframe_html_path.write_text(iframe_html, encoding="utf-8")
            print_info(f"iframe HTML: {iframe_html_path.name}")
        else:
            # Method 2: Try to get iframe by ID
            iframe_element = page.locator("#unified-webclient-iframe")
            if await iframe_element.count() > 0:
                # Get frame from all available frames
                all_frames = page.frames
                for f in all_frames:
                    if f != page.main_frame:
                        try:
                            iframe_html = await f.content()
                            iframe_html_path = output_dir / f"{prefix}_iframe.html"
                            iframe_html_path.write_text(iframe_html, encoding="utf-8")
                            print_info(f"iframe HTML: {iframe_html_path.name}")
                            break
                        except Exception:
                            continue

            if not iframe_html_path:
                # List available frames for debugging
                all_frames = page.frames
                if len(all_frames) > 1:
                    print_info(f"iframe HTML: (found {len(all_frames) - 1} frame(s), but could not extract)")
                else:
                    print_info("iframe HTML: (no iframe found at this stage)")
    except Exception as e:
        # iframe content extraction may fail due to cross-origin restrictions
        print_info(f"iframe HTML: (extraction failed: {e})")

    # 4. Page title for quick reference
    try:
        page_title = await page.title()
        print_info(f"Page title: {page_title}")
    except Exception:
        page_title = "N/A"

    # 5. Debug summary
    try:
        debug_path = output_dir / f"{prefix}_debug.txt"
        debug_info = f"""=== Debug Info ===
Step: {step_name}
Time: {utc_now().isoformat()}
URL: {page.url}
Title: {page_title}
Error: {error_msg or "User requested debug dump"}

Files:
- Screenshot: {prefix}.png
- HTML: {prefix}.html
{f"- iframe HTML: {prefix}_iframe.html" if iframe_html_path else "- iframe HTML: (not available)"}

Hint: Check the HTML file for expected element selectors
"""
        debug_path.write_text(debug_info, encoding="utf-8")
        print_info(f"Debug summary: {debug_path.name}")
    except Exception as e:
        print_error(f"Failed to save debug summary: {e}")

    print_success(f"Debug info saved to {output_dir}")


async def interactive_prompt(page, output_dir: Path, step_name: str) -> str:
    """Interactive prompt for user input.

    Returns:
        'continue' - proceed to next step
        'error' - user marked as error, debug info dumped
        'skip' - skip this step
    """
    print(f"\n  [Interactive] Step '{step_name}' completed")
    print("  Input: [Enter]=continue | e=error & exit | html=dump HTML | skip=skip step")

    response = await asyncio.get_event_loop().run_in_executor(None, lambda: input("  > "))
    response = response.strip().lower()

    if response in ("e", "error"):
        await dump_debug_info(page, output_dir, step_name, "User marked as error")
        return "error"
    elif response == "html":
        await dump_debug_info(page, output_dir, step_name, "User requested debug dump")
        print_info("Debug info dumped, continuing...")
        return "continue"
    elif response == "skip":
        print_info("Skipping step...")
        return "skip"
    else:
        return "continue"


async def run_test(page, provider, args, output_dir: Path, timestamp: str) -> bool:
    """Run the provider test flow.

    Returns:
        True if test succeeded, False otherwise
    """
    interactive = args.interactive

    # Step 1: Navigate to meeting URL
    print_step(1, "Navigating to meeting URL")

    # Apply muted config for Jitsi URLs (prevents auto-enabling video/audio)
    meeting_url = append_jitsi_muted_config(args.url)
    print_info(f"URL: {meeting_url}")

    try:
        await page.goto(meeting_url, wait_until="domcontentloaded", timeout=30000)
        print_success("Page loaded")
        await take_screenshot(page, output_dir, f"{timestamp}_01_navigate")
    except Exception as e:
        print_error(f"Failed to load page: {e}")
        await dump_debug_info(page, output_dir, "01_navigate", str(e))
        return False

    if interactive:
        result = await interactive_prompt(page, output_dir, "navigate")
        if result == "error":
            return False

    # Step 2: Handle prejoin page
    print_step(2, "Handling prejoin page")
    print_info(f"Display name: {args.name}")

    try:
        await provider.prejoin(page, args.name, args.password)
        print_success("Prejoin completed")
        await take_screenshot(page, output_dir, f"{timestamp}_02_prejoin")
    except Exception as e:
        print_error(f"Prejoin failed: {e}")
        await dump_debug_info(page, output_dir, "02_prejoin", str(e))
        return False

    if interactive:
        result = await interactive_prompt(page, output_dir, "prejoin")
        if result == "error":
            return False

    # Step 3: Click join button
    print_step(3, "Clicking join button")

    try:
        await provider.click_join(page)
        print_success("Join button clicked")
        await take_screenshot(page, output_dir, f"{timestamp}_03_after_join")
    except Exception as e:
        print_error(f"Failed to click join: {e}")
        await dump_debug_info(page, output_dir, "03_join", str(e))
        return False

    if interactive:
        result = await interactive_prompt(page, output_dir, "join")
        if result == "error":
            return False

    # Step 4: Wait for join result
    print_step(4, f"Waiting for join result (timeout={args.timeout}s)")

    result = await provider.wait_until_joined(page, args.timeout, args.password)
    await take_screenshot(page, output_dir, f"{timestamp}_04_final")

    # Print result
    print_step(5, "Test Result")

    if result.success:
        print_success("Successfully joined meeting!")
        if interactive:
            await interactive_prompt(page, output_dir, "in_meeting")
        return True
    elif result.in_lobby:
        print_info("In lobby/waiting room (not admitted yet)")
        print_info("This is expected if the meeting requires host approval")
        # Auto-dump lobby debug info for detection development
        await dump_debug_info(page, output_dir, "lobby")
        print_info("Lobby HTML dumped for analysis")

        if interactive:
            lobby_result = await interactive_prompt(page, output_dir, "lobby")

            # Step 6: Wait until actually in meeting (for Webex lobby flow)
            if lobby_result != "skip":
                print_step(6, "Waiting until admitted to meeting...")
                print_info("Press Enter to re-check status, or 'skip' to exit early")
                print_info("Have the host admit you to the meeting, then press Enter")

                # Keep checking until in meeting or user skips
                max_attempts = 60  # Max 60 checks (with user interaction)
                for attempt in range(max_attempts):
                    # Wait for user to signal they're ready to check
                    user_input = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda a=attempt: input(f"  [{a + 1}] Check status? [Enter]=check | skip=exit > ")
                        .strip()
                        .lower(),
                    )

                    if user_input == "skip":
                        print_info("Skipping in-meeting wait phase")
                        break
                    elif user_input == "html":
                        await dump_debug_info(page, output_dir, f"check_{attempt + 1}")
                        continue

                    # Re-check status
                    print_info("Checking meeting status...")
                    check_result = await provider.wait_until_joined(page, timeout_sec=5)

                    if check_result.success:
                        print_success("Successfully joined meeting!")
                        await take_screenshot(page, output_dir, f"{timestamp}_06_in_meeting")
                        await dump_debug_info(page, output_dir, "in_meeting")
                        print_info("In-meeting HTML dumped for analysis")

                        if interactive:
                            await interactive_prompt(page, output_dir, "in_meeting")
                        return True
                    elif check_result.in_lobby:
                        print_info("Still in lobby, waiting for host...")
                    else:
                        print_error(f"Status check failed: {check_result.error_message}")
                        break

        return True  # Consider lobby as partial success
    else:
        print_error("Failed to join meeting")
        if result.error_code:
            print_error(f"Error code: {result.error_code}")
        if result.error_message:
            print_error(f"Error message: {result.error_message}")
        await dump_debug_info(page, output_dir, "04_final", result.error_message)
        return False


async def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    args = parse_args()

    # Setup output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d_%H%M%S")

    # Print test configuration
    print("\n" + "=" * 60)
    print("  Provider Test Script")
    print("=" * 60)
    print(f"  Provider:    {args.provider}")
    print(f"  URL:         {args.url}")
    print(f"  Name:        {args.name}")
    print(f"  Timeout:     {args.timeout}s")
    print(f"  Interactive: {args.interactive}")
    print(f"  Slow-mo:     {args.slowmo}ms")
    print(f"  Output dir:  {output_dir.absolute()}")
    print("=" * 60)

    # Get provider
    try:
        provider = get_provider(args.provider)
        print_info(f"Using provider: {provider.name}")
    except ValueError as e:
        print_error(str(e))
        return 1

    # Launch browser
    async with async_playwright() as p:
        print_info("Launching browser (headless=False)...")

        browser = await p.chromium.launch(
            headless=False,
            slow_mo=args.slowmo if args.slowmo > 0 else None,
            args=[
                # Use fake media devices instead of real hardware
                # "--use-fake-device-for-media-stream",    # should be removed, or user may see fake camera + microphone UI in Webex
                # "--use-fake-ui-for-media-stream",        # should be removed, or user may see fake camera UI in Webex
                # Disable real media capture
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
                # Window configuration
                "--window-size=1920,1080",
                "--window-position=0,0",
                # Performance settings
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            permissions=[
                "microphone"
            ],  # Permissions should not be removed, due to Webex requires permissions to join the meeting
        )
        page = await context.new_page()

        try:
            success = await run_test(page, provider, args, output_dir, timestamp)
        except KeyboardInterrupt:
            print_info("\nTest interrupted by user")
            success = False
        except Exception as e:
            print_error(f"Unexpected error: {e}")
            import traceback

            traceback.print_exc()
            success = False
        finally:
            if args.interactive:
                await wait_for_user("Test finished. Press Enter to close browser...")
            await browser.close()

    # Final summary
    print("\n" + "=" * 60)
    if success:
        print("  TEST PASSED")
    else:
        print("  TEST FAILED")
    print("=" * 60)
    print(f"  Screenshots saved to: {output_dir.absolute()}")
    print("=" * 60 + "\n")

    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
