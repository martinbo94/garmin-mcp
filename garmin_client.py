"""Garmin Connect client - handles authentication and workout upload."""
from typing import Dict, List, Optional
from garminconnect import Garmin, GarminConnectAuthenticationError


class GarminClient:
    """Client for interacting with Garmin Connect API."""

    def __init__(self, email: str, password: str):
        """Initialize Garmin client.

        Args:
            email: Garmin Connect email
            password: Garmin Connect password
        """
        self.email = email
        self.password = password
        self.client: Optional[Garmin] = None

    def connect(self) -> bool:
        """Authenticate with Garmin Connect.

        Returns:
            True if successful, False otherwise
        """
        try:
            self.client = Garmin(self.email, self.password)
            self.client.login()
            print("[OK] Successfully connected to Garmin Connect")
            return True
        except GarminConnectAuthenticationError as e:
            print(f"[ERROR] Authentication failed: {e}")
            return False
        except Exception as e:
            print(f"[ERROR] Connection error: {e}")
            return False

    def test_connection(self) -> bool:
        """Test Garmin Connect connection.

        Returns:
            True if connection works, False otherwise
        """
        if not self.client:
            return self.connect()

        try:
            # Simple API call to test connection
            self.client.get_user_summary(None)
            print("[OK] Connection test successful")
            return True
        except Exception as e:
            print(f"[ERROR] Connection test failed: {e}")
            return False

    def list_workouts(self, limit: int = 100, silent: bool = False) -> List[Dict]:
        """List existing workouts on Garmin Connect.

        Args:
            limit: Maximum number of workouts to retrieve (default: 100)
            silent: If True, don't print the list (default: False)

        Returns:
            List of workout dictionaries
        """
        if not self.client:
            if not self.connect():
                return []

        try:
            # Use direct API endpoint (garminconnect library's get_workouts() is broken)
            url = f"{self.client.garmin_workouts}/workouts"
            response = self.client.garth.get("connectapi", url, api=True)
            workouts = response.json()

            # Handle response format
            if isinstance(workouts, dict) and 'workouts' in workouts:
                workouts = workouts['workouts']
            elif not isinstance(workouts, list):
                workouts = []

            # Apply limit
            workouts = workouts[:limit]

            if not silent:
                print(f"\nFound {len(workouts)} workout template(s):")

                if workouts:
                    for i, workout in enumerate(workouts, 1):
                        name = workout.get('workoutName', 'Unnamed')
                        sport = workout.get('sportType', {}).get('sportTypeKey', 'unknown')
                        workout_id = workout.get('workoutId', 'N/A')
                        print(f"  {i}. {name} ({sport}) [ID: {workout_id}]")
                else:
                    print("  (No workout templates found)")

            return workouts
        except Exception as e:
            if not silent:
                print(f"[ERROR] Failed to list workouts: {e}")
            return []

    def upload_workout(self, workout_data: Dict, return_id: bool = False):
        """Upload workout to Garmin Connect.

        Args:
            workout_data: Workout dictionary from WorkoutGenerator
            return_id: If True, return workout ID instead of boolean

        Returns:
            If return_id=False: True if successful, False otherwise
            If return_id=True: workout ID if successful, None otherwise
        """
        if not self.client:
            if not self.connect():
                return None if return_id else False

        try:
            garmin_workout = self._format_for_garmin(workout_data)
            result = self.client.upload_workout(garmin_workout)
            print(f"[OK] Uploaded: {workout_data['workout_name']}")

            if return_id:
                # Extract workout ID from response
                workout_id = result.get('workoutId')
                return workout_id
            return True
        except Exception as e:
            print(f"[ERROR] Failed to upload {workout_data['workout_name']}: {e}")
            return None if return_id else False

    def delete_workout(self, workout_id: int) -> bool:
        """Delete a workout template entirely from Garmin Connect.

        Args:
            workout_id: ID of the workout template to delete

        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            if not self.connect():
                return False

        try:
            url = f"{self.client.garmin_workouts}/workout/{workout_id}"
            self.client.garth.delete("connectapi", url, api=True)
            return True
        except Exception:
            return False

    def delete_workout_by_name(self, workout_name: str, prefix_match: bool = False) -> int:
        """Delete workout templates by name.

        This is useful for removing duplicate templates before uploading new ones.
        Deleting a template automatically unschedules it from the calendar.

        Args:
            workout_name: Name to match
            prefix_match: If True, delete all workouts starting with workout_name.
                         Useful when workout name format changes (e.g., "Week 1 - tuesday - Strength"
                         will match both "Week 1 - tuesday - Strength" and
                         "Week 1 - tuesday - Strength (Pullups, ...)")

        Returns:
            Number of workout templates deleted
        """
        if not self.client:
            if not self.connect():
                return 0

        try:
            # Get all workout templates
            workouts = self.list_workouts(limit=100, silent=True)
            deleted_count = 0

            # Find and delete matches
            for workout in workouts:
                name = workout.get('workoutName', '')

                # Use prefix or exact matching
                if prefix_match:
                    match = name.startswith(workout_name)
                else:
                    match = (name == workout_name)

                if match:
                    workout_id = workout.get('workoutId')
                    if workout_id and self.delete_workout(workout_id):
                        deleted_count += 1

            return deleted_count
        except Exception as e:
            print(f"[WARNING] Error deleting workouts by name: {e}")
            return 0

    def cleanup_old_workouts(self, current_week: int) -> int:
        """Delete workout templates from previous weeks (not current week).

        This helps prevent clutter by removing workout templates from previous weeks.
        Only deletes workouts with names like "Week X - Day - Type" where X != current_week.

        Safety features:
        - Only deletes workouts matching exact pattern "Week X - dayname - Type"
        - Skips current week's workouts
        - Manual workouts (like "6x4", "6km tempo") are never deleted

        Args:
            current_week: Current week number (e.g., 1, 2, 3...)

        Returns:
            Number of workouts deleted
        """
        if not self.client:
            if not self.connect():
                return 0

        try:
            import re

            # Get all workouts using working API endpoint (silently)
            workouts = self.list_workouts(limit=100, silent=True)
            deleted_count = 0

            for workout in workouts:
                workout_name = workout.get('workoutName', '')
                workout_id = workout.get('workoutId')

                # Safety check 1: Must match EXACT pattern "Week X - dayname - Type"
                # This protects manual workouts like "6x4", "6km tempo i langtur", etc.
                match = re.match(r'^Week (\d+) - \w+ - ', workout_name)
                if not match:
                    continue  # Not our generated workout - skip

                # Safety check 2: Extract week number and only delete if NOT current week
                workout_week = int(match.group(1))
                if workout_week == current_week:
                    continue  # This is current week - skip

                # Safe to delete - it's a previous week's workout
                if self.delete_workout(workout_id):
                    deleted_count += 1
                    print(f"  [DELETED] {workout_name} (Week {workout_week})")

            return deleted_count
        except Exception as e:
            print(f"[WARNING] Cleanup encountered error: {e}")
            return 0

    def upload_and_schedule_workout(self, workout_data: Dict, date_str: str, replace_existing: bool = True) -> bool:
        """Upload a workout and immediately schedule it to a date.

        Args:
            workout_data: Workout dictionary from WorkoutGenerator
            date_str: Date in format "YYYY-MM-DD"
            replace_existing: If True, delete any existing templates with the same name first

        Returns:
            True if both upload and schedule successful, False otherwise
        """
        # Delete any existing templates with this name (smart prefix match)
        if replace_existing:
            import re
            workout_name = workout_data['workout_name']

            # Extract base prefix: "Week X - dayname - " to catch all name variations
            # This matches both old formats like "Week 1 - wednesday - Easy Endurance"
            # and new formats like "Week 1 - wednesday - 45-60min Easy Z1-2 (...)"
            match = re.match(r'^(Week \d+ - \w+ - )', workout_name)
            if match:
                prefix = match.group(1)
                deleted = self.delete_workout_by_name(prefix, prefix_match=True)
            else:
                # Fallback to full name prefix matching if pattern doesn't match
                deleted = self.delete_workout_by_name(workout_name, prefix_match=True)

            if deleted > 0:
                print(f"    [REMOVED] {deleted} existing template(s) for this day")

        # Upload and get ID
        workout_id = self.upload_workout(workout_data, return_id=True)

        if not workout_id:
            return False

        # Schedule it
        return self.schedule_workout(workout_id, date_str)

    def schedule_workout(self, workout_id: int, date_str: str) -> bool:
        """Schedule a workout to a specific date on the calendar.

        Args:
            workout_id: ID of the uploaded workout
            date_str: Date in format "YYYY-MM-DD"

        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            if not self.connect():
                return False

        try:
            # The schedule endpoint requires the date in a specific format
            url = f"{self.client.garmin_workouts}/schedule/{workout_id}"
            payload = {"date": date_str}

            # Use garth to make the API call
            self.client.garth.post("connectapi", url, json=payload, api=True)
            print(f"[OK] Scheduled workout {workout_id} for {date_str}")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to schedule workout: {e}")
            return False

    def _get_garmin_sport_type(self, sport_type_key: str) -> Dict:
        """Convert sport type to Garmin sport type structure.

        Args:
            sport_type_key: Internal sport type key

        Returns:
            Garmin sport type dictionary with sportTypeId and sportTypeKey
        """
        # Map to Garmin sport type keys
        # Note: "ski_erg" maps to "cardio" since SkiErg structured workouts
        # aren't natively supported by Garmin's workout API
        garmin_sport_key_map = {
            "running": "running",
            "treadmill_running": "running",
            "ski_erg": "cardio",
            "strength_training": "strength_training",
            "other": "other"
        }
        garmin_sport_key = garmin_sport_key_map.get(sport_type_key, "other")

        # Sport type IDs for Garmin API
        sport_type_id_map = {
            "running": 1,
            "strength_training": 5,
            "cardio": 6,
            "other": 0
        }
        sport_type_id = sport_type_id_map.get(garmin_sport_key, 0)

        return {
            "sportTypeId": sport_type_id,
            "sportTypeKey": garmin_sport_key,
            "displayOrder": 1
        }

    def _format_for_garmin(self, workout_data: Dict) -> Dict:
        """Convert workout data to Garmin Connect format.

        Args:
            workout_data: Workout dictionary from WorkoutGenerator

        Returns:
            Garmin-formatted workout dictionary
        """
        workout_type = workout_data.get("workout_type")
        sport_type = workout_data.get("sport_type", "other")

        # Get proper sport type structure with both ID and key
        sport_type_struct = self._get_garmin_sport_type(sport_type)

        garmin_workout = {
            "workoutName": workout_data["workout_name"],
            "sportType": sport_type_struct,
            "workoutSegments": []
        }

        # Build workout segments based on workout type
        if workout_type in ["vo2max_intervals", "threshold_intervals"]:
            garmin_workout["workoutSegments"] = self._build_interval_segments(workout_data)

        elif workout_type in ["easy_endurance", "rest_or_recovery", "long_run", "strength"]:
            # Strength workouts use simple segments (just a timed reminder with "Other" sport type)
            garmin_workout["workoutSegments"] = self._build_simple_segments(workout_data)

        return garmin_workout

    def _build_interval_segments(self, workout_data: Dict) -> List[Dict]:
        """Build workout segments for interval workouts.

        Args:
            workout_data: Workout data with warmup, main_set, cooldown

        Returns:
            List of Garmin workout segments
        """
        # Get sport type with proper structure
        sport_type_key = workout_data.get("sport_type", "running")
        sport_type = self._get_garmin_sport_type(sport_type_key)

        # All steps go in one segment
        all_steps = []
        step_order = 1

        # Warmup step
        warmup = workout_data.get("warmup", {})
        all_steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": {
                "stepTypeId": 1,
                "stepTypeKey": "warmup",
                "displayOrder": 1
            },
            "endCondition": {
                "conditionTypeId": 2,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True
            },
            "endConditionValue": float(warmup.get("duration_min", 15) * 60),
            "targetType": {
                "workoutTargetTypeId": 4,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4
            },
            "targetValueOne": float(warmup.get("hr_range", {}).get("min", 120)),
            "targetValueTwo": float(warmup.get("hr_range", {}).get("max", 140))
        })
        step_order += 1

        # Main set - repeat group
        main_set = workout_data.get("main_set", {})
        intervals = main_set.get("intervals", 4)
        work_duration = main_set.get("work_duration_min", 5)
        rest_duration = main_set.get("rest_duration_min", 2.5)
        work_hr = main_set.get("work_hr_range", {})
        rest_hr = main_set.get("rest_hr_range", {})

        # Create repeat steps
        repeat_steps = []

        # Work interval
        repeat_steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": step_order + 1,
            "childStepId": 1,
            "stepType": {
                "stepTypeId": 3,
                "stepTypeKey": "interval",
                "displayOrder": 3
            },
            "endCondition": {
                "conditionTypeId": 2,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True
            },
            "endConditionValue": float(work_duration * 60),
            "targetType": {
                "workoutTargetTypeId": 4,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4
            },
            "targetValueOne": float(work_hr.get("min", 160)),
            "targetValueTwo": float(work_hr.get("max", 180))
        })

        # Recovery interval
        repeat_steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": step_order + 2,
            "childStepId": 1,
            "stepType": {
                "stepTypeId": 4,
                "stepTypeKey": "recovery",
                "displayOrder": 4
            },
            "endCondition": {
                "conditionTypeId": 2,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True
            },
            "endConditionValue": float(rest_duration * 60),
            "targetType": {
                "workoutTargetTypeId": 4,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4
            },
            "targetValueOne": float(rest_hr.get("min", 110)),
            "targetValueTwo": float(rest_hr.get("max", 140))
        })

        # Add repeat group
        all_steps.append({
            "type": "RepeatGroupDTO",
            "stepOrder": step_order,
            "childStepId": 1,
            "stepType": {
                "stepTypeId": 6,
                "stepTypeKey": "repeat",
                "displayOrder": 6
            },
            "numberOfIterations": intervals,
            "smartRepeat": False,
            "workoutSteps": repeat_steps
        })
        step_order += 3

        # Cooldown step
        cooldown = workout_data.get("cooldown", {})
        all_steps.append({
            "type": "ExecutableStepDTO",
            "stepOrder": step_order,
            "stepType": {
                "stepTypeId": 2,
                "stepTypeKey": "cooldown",
                "displayOrder": 2
            },
            "endCondition": {
                "conditionTypeId": 2,
                "conditionTypeKey": "time",
                "displayOrder": 2,
                "displayable": True
            },
            "endConditionValue": float(cooldown.get("duration_min", 10) * 60),
            "targetType": {
                "workoutTargetTypeId": 4,
                "workoutTargetTypeKey": "heart.rate.zone",
                "displayOrder": 4
            },
            "targetValueOne": float(cooldown.get("hr_range", {}).get("min", 110)),
            "targetValueTwo": float(cooldown.get("hr_range", {}).get("max", 140))
        })

        return [{
            "segmentOrder": 1,
            "sportType": sport_type,
            "workoutSteps": all_steps
        }]

    def _build_simple_segments(self, workout_data: Dict) -> List[Dict]:
        """Build segments for simple duration-based workouts.

        Args:
            workout_data: Workout data with duration and HR range

        Returns:
            List of Garmin workout segments
        """
        duration_min = workout_data.get("duration_min", 30)
        hr_range = workout_data.get("hr_range", {"min": 120, "max": 140})

        # Get sport type with proper structure
        sport_type_key = workout_data.get("sport_type", "running")
        sport_type = self._get_garmin_sport_type(sport_type_key)

        return [{
            "segmentOrder": 1,
            "sportType": sport_type,
            "workoutSteps": [{
                "type": "ExecutableStepDTO",
                "stepOrder": 1,
                "stepType": {
                    "stepTypeId": 3,
                    "stepTypeKey": "interval",
                    "displayOrder": 3
                },
                "endCondition": {
                    "conditionTypeId": 2,
                    "conditionTypeKey": "time",
                    "displayOrder": 2,
                    "displayable": True
                },
                "endConditionValue": float(duration_min * 60),
                "targetType": {
                    "workoutTargetTypeId": 4,
                    "workoutTargetTypeKey": "heart.rate.zone",
                    "displayOrder": 4
                },
                "targetValueOne": float(hr_range.get("min", 120)),
                "targetValueTwo": float(hr_range.get("max", 140))
            }]
        }]

    def _build_strength_segments(self, workout_data: Dict) -> List[Dict]:
        """Build minimal segment for strength workout reminders.

        Creates a simple timed strength workout with no specific exercises.
        This is meant to be a calendar reminder only.

        Args:
            workout_data: Workout data with duration

        Returns:
            List of Garmin workout segments (minimal structure)
        """
        duration_min = workout_data.get("duration_min", 45)

        # Create one simple "workout" step (Norwegian: "trening")
        return [{
            "segmentOrder": 1,
            "sportType": {"sportTypeKey": "strength_training"},
            "workoutSteps": [{
                "type": "WorkoutStep",
                "stepOrder": 1,
                "stepType": {"stepTypeKey": "workout"},  # "trening" type
                "endCondition": {
                    "conditionTypeKey": "time",
                    "conditionTypeId": 2
                },
                "endConditionValue": duration_min * 60,  # Convert to seconds
                "targetType": {"workoutTargetTypeKey": "no.target"}
            }]
        }]
