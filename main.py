"""Main script to generate and upload weekly workouts to Garmin Connect."""
import os
from dotenv import load_dotenv
from workout_generator import WorkoutGenerator, load_program
from garmin_client import GarminClient
import config


def test_connection():
    """Test Garmin Connect connection."""
    load_dotenv(override=True)

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        print("✗ Error: GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env file")
        return

    print("\nTesting Garmin Connect connection...")
    client = GarminClient(email, password)

    if client.test_connection():
        print("\n✓ Connection test successful!")
        print("\nFetching recent workouts...")
        client.list_workouts(5)
    else:
        print("\n✗ Connection test failed.")


def list_workouts():
    """List recent workouts from Garmin Connect."""
    load_dotenv(override=True)

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        print("[ERROR] GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env file")
        return

    client = GarminClient(email, password)
    client.list_workouts(10)


def schedule_current_week():
    """Schedule workouts for the current week only with automatic cleanup."""
    load_dotenv(override=True)
    from datetime import datetime, timedelta

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        print("✗ Error: GARMIN_EMAIL and GARMIN_PASSWORD must be set in .env file")
        return

    # Load program first to get structure
    try:
        program = load_program("program.json")
    except Exception as e:
        print(f"✗ Failed to load program: {e}")
        return

    # Configuration
    DAYS = list(program.weekly_structure.__class__.model_fields.keys())

    # Get current week number from config
    week_number = config.get_current_week()

    # Calculate Monday of current week
    today = datetime.now()
    days_since_monday = today.weekday()  # 0 = Monday, 6 = Sunday
    monday_this_week = today - timedelta(days=days_since_monday)

    print(f"\n{'='*60}")
    print(f"SCHEDULING CURRENT WEEK")
    print(f"{'='*60}")
    print(f"Week {week_number} of training program")
    print(f"Monday: {monday_this_week.strftime('%A, %B %d, %Y')}")
    print(f"{'='*60}\n")

    print(f"✓ Loaded program: {program.program_name}")

    # Initialize generator
    max_hr = config.MAX_HEART_RATE
    generator = WorkoutGenerator(program, max_hr)

    # Connect to Garmin
    print(f"\nConnecting to Garmin Connect...")
    client = GarminClient(email, password)
    if not client.connect():
        print("\n✗ Failed to connect to Garmin Connect")
        return

    # Cleanup old workouts first (delete previous weeks, keep current week)
    print(f"\n{'='*60}")
    print("CLEANING UP OLD WORKOUTS")
    print(f"{'='*60}")
    deleted_count = client.cleanup_old_workouts(current_week=week_number)
    if deleted_count > 0:
        print(f"✓ Removed {deleted_count} old workout template(s) from previous weeks")
    else:
        print(f"✓ No old workouts to clean up (keeping Week {week_number} templates)")

    # Generate and schedule workouts
    # Note: All workouts are now hardcoded in program.json:
    # - Wednesday/Sunday: running (calendar reminders only)
    # - Saturday: ski_erg (maps to cardio in Garmin API)
    print(f"\n{'='*60}")
    print(f"GENERATING & SCHEDULING WORKOUTS")
    print(f"{'='*60}\n")

    total_workouts = 0
    successful_uploads = 0

    for day_index, day_name in enumerate(DAYS):
        workout_date = monday_this_week + timedelta(days=day_index)
        date_str = workout_date.strftime("%Y-%m-%d")

        print(f"{day_name.upper()} ({workout_date.strftime('%b %d')}):")

        # Generate workouts for this day (modality is hardcoded in program.json)
        workouts = generator.generate_day(day_name, week_number)

        if not workouts:
            print(f"  [REST DAY]\n")
            continue

        # Delete ALL old workouts for this day ONCE (before uploading any)
        # This prevents deleting workouts we just uploaded when there are multiple per day
        import re
        first_workout_name = workouts[0]['workout_name']
        match = re.match(r'^(Week \d+ - \w+ - )', first_workout_name)
        if match:
            prefix = match.group(1)
            deleted = client.delete_workout_by_name(prefix, prefix_match=True)
            if deleted > 0:
                print(f"  [CLEANUP] Removed {deleted} old workout(s) for this day\n")

        # Now upload all workouts for this day (without per-workout deletion)
        for workout in workouts:
            total_workouts += 1
            workout_name = workout['workout_name']
            workout_type = workout.get('workout_type', 'unknown')

            # Add type indicator for clarity
            type_indicator = ""
            if workout_type == 'strength':
                type_indicator = " [Calendar Reminder]"

            print(f"  {workout_name}{type_indicator}")

            # Upload and schedule (replace_existing=False since we already cleaned up above)
            if client.upload_and_schedule_workout(workout, date_str, replace_existing=False):
                successful_uploads += 1
                print(f"    ✓ Scheduled for {date_str}")
            else:
                print(f"    ✗ Failed to schedule")

        print()

    # Summary
    print(f"{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Total workouts: {total_workouts}")
    print(f"Successfully scheduled: {successful_uploads}")
    print(f"Failed: {total_workouts - successful_uploads}")
    print(f"{'='*60}")
    print(f"\n✓ Week {week_number} is now scheduled on your Garmin!")
    print(f"✓ Check Garmin Connect: Training > Calendar")
    print(f"✓ Your watch will suggest these workouts automatically")
    print(f"   when you start the matching activity type.")
    print(f"\n💡 Tip: Run this script again next week to schedule Week {week_number + 1}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    # Main function - schedule current week's workouts
    schedule_current_week()

    # Utility functions (uncomment to use):
    # test_connection()  # Test Garmin Connect connection
    # list_workouts()    # List existing workout templates
