"""Workout generator - creates workouts based on program structure and week number."""
import json
from typing import Dict, List, Literal, Optional
from models import Program, DayWorkout, IntervalVariant


class WorkoutGenerator:
    """Generates workouts based on training program and week number."""

    def __init__(self, program: Program, max_heart_rate: int):
        """Initialize generator with program and max heart rate.

        Args:
            program: Training program structure
            max_heart_rate: Maximum heart rate for calculating zones
        """
        self.program = program
        self.max_hr = max_heart_rate
        self.hr_zones = self._calculate_hr_zones()

    def _calculate_hr_zones(self) -> Dict[int, Dict[str, int]]:
        """Calculate heart rate zones based on max HR.

        Returns:
            Dictionary mapping zone numbers to {min, max} HR values
        """
        zones = {}
        for i in range(1, 6):
            zone_key = f"zone_{i}"
            zone = getattr(self.program.heart_rate_zones, zone_key)
            zones[i] = {
                "min": int(self.max_hr * zone.percent_min / 100),
                "max": int(self.max_hr * zone.percent_max / 100)
            }
        return zones

    def _get_vo2max_variant(self, week_num: int, day: Literal["tuesday", "thursday"]) -> str:
        """Get VO2max variant based on week number and day.

        Logic:
        - Even weeks: Tuesday=A (4x6), Thursday=B (5x5)
        - Odd weeks: Tuesday=C (6x4), Thursday=D (4x8)

        Args:
            week_num: Week number (1-12)
            day: Which day (tuesday or thursday)

        Returns:
            Variant letter (A, B, C, or D)
        """
        if week_num % 2 == 0:  # Even weeks
            return "A" if day == "tuesday" else "B"
        else:  # Odd weeks
            return "C" if day == "tuesday" else "D"

    def _get_threshold_variant(self, week_num: int) -> str:
        """Get threshold interval variant based on week number.

        Args:
            week_num: Week number (1-12)

        Returns:
            Variant letter (A or B)
        """
        # Weeks 1-4: 3 intervals (variant A)
        # Weeks 5-12: 4 intervals (variant B)
        if week_num <= 4:
            return "A"
        return "B"

    def _get_duration_for_phase(self, week_num: int, base_duration: int) -> int:
        """Get workout duration based on periodization phase if applicable.

        Args:
            week_num: Week number (1-12)
            base_duration: Base duration from day_workout

        Returns:
            Duration in minutes (from periodization if long_run, else base)
        """
        # Check if this workout type uses periodization-based duration
        # Currently only long_run has phase-specific durations
        if hasattr(self.program.periodization, 'phase_1') and hasattr(self.program.periodization.phase_1, 'long_run_duration_min'):
            if week_num <= 4:
                return self.program.periodization.phase_1.long_run_duration_min
            elif week_num <= 8:
                return self.program.periodization.phase_2.long_run_duration_min
            else:
                return self.program.periodization.phase_3.long_run_duration_min

        # Default: use base duration from day_workout
        return base_duration

    def generate_day(self, day_name: str, week_num: int) -> List[Dict]:
        """Generate workout(s) for a specific day and week.

        Returns list because some days (Tuesday/Thursday) have 2 separate workouts.

        Args:
            day_name: Name of day (monday, tuesday, etc.)
            week_num: Week number (1-12)

        Returns:
            List of workout dictionaries ready for Garmin formatting
        """
        day_workout: DayWorkout = getattr(self.program.weekly_structure, day_name.lower())

        # Skip rest days
        if day_workout.workout_type == "rest":
            return []

        # Handle multi-part workouts (e.g., intervals + strength)
        if day_workout.parts:
            return self._generate_multi_part_workout(day_name, week_num, day_workout)

        # Single workout
        return [self._generate_single_workout(day_name, week_num, day_workout)]

    def _generate_single_workout(self, day_name: str, week_num: int, day_workout: DayWorkout) -> Dict:
        """Generate a single workout based on its structure in program.json.

        Args:
            day_name: Name of day
            week_num: Week number
            day_workout: DayWorkout model from program

        Returns:
            Workout dictionary
        """
        workout_type = day_workout.workout_type

        # Calendar reminder workouts (simple structure)
        if workout_type in ["rest_or_recovery", "easy_endurance", "long_run"]:
            return self._generate_calendar_reminder(day_name, week_num, day_workout)

        # Structured interval workouts
        elif workout_type in ["vo2max_intervals", "threshold_intervals"]:
            return self._generate_interval_workout(day_name, week_num, day_workout)

        return {}

    def _generate_multi_part_workout(self, day_name: str, week_num: int, day_workout: DayWorkout) -> List[Dict]:
        """Generate multi-part workouts (e.g., intervals + strength).

        Args:
            day_name: Name of day
            week_num: Week number
            day_workout: DayWorkout model with parts

        Returns:
            List of workout dictionaries
        """
        workouts = []

        for part in day_workout.parts:
            part_type = part.type

            # Interval parts (vo2max, threshold)
            if "intervals" in part_type:
                # Determine variant based on workout type
                if "vo2max" in part_type:
                    variant_letter = self._get_vo2max_variant(week_num, day_name.lower())
                elif "threshold" in part_type:
                    variant_letter = self._get_threshold_variant(week_num)
                else:
                    variant_letter = "A"  # Default fallback

                variant = next(v for v in part.structure.main_set_variants if v.variant == variant_letter)

                workouts.append({
                    "workout_name": f"Week {week_num} - {day_name} - {part_type.replace('_', ' ').title()} {variant_letter}",
                    "day": day_name.capitalize(),
                    "week": week_num,
                    "workout_type": part_type,
                    "sport_type": self._get_sport_type(part.modality),
                    "variant": variant_letter,
                    "warmup": {
                        "duration_min": part.structure.warmup_duration_min,
                        "hr_range": self.hr_zones[part.structure.warmup_intensity_zone]
                    },
                    "main_set": {
                        "intervals": variant.intervals,
                        "work_duration_min": variant.work_duration_min,
                        "rest_duration_min": variant.rest_duration_min,
                        "work_hr_range": self._get_zone_range(part.structure.work_intensity_zones),
                        "rest_hr_range": self._get_zone_range(part.structure.rest_intensity_zones)
                    },
                    "cooldown": {
                        "duration_min": part.structure.cooldown_duration_min,
                        "hr_range": self._get_zone_range(part.structure.cooldown_intensity_zones)
                    }
                })

            # Strength parts - create calendar reminder
            elif "strength" in part_type:
                workouts.append({
                    "workout_name": f"Week {week_num} - {day_name} - Strength",
                    "day": day_name.capitalize(),
                    "week": week_num,
                    "workout_type": "strength",
                    "sport_type": "strength_training",
                    "duration_min": part.duration_min
                })

        return workouts

    def _generate_calendar_reminder(self, day_name: str, week_num: int, day_workout: DayWorkout) -> Dict:
        """Generate simple calendar reminder workout.

        Used for: easy endurance, long runs, recovery, strength

        All workout information comes from program.json. Optional duration_display
        field can specify flexible duration (e.g., "45-60min" instead of exact "55min").

        Args:
            day_name: Name of day
            week_num: Week number
            day_workout: DayWorkout model

        Returns:
            Workout dictionary
        """
        workout_type = day_workout.workout_type

        # Get duration (check periodization for phase-based adjustments like long runs)
        duration = self._get_duration_for_phase(week_num, day_workout.duration_min)

        # Check for optional duration_display field (e.g., "45-60min")
        duration_display = getattr(day_workout, 'duration_display', None) or f"{duration}min"

        # Get zones and format zone string
        zones = day_workout.intensity_zones if day_workout.intensity_zones else [2]
        hr_range = self._get_zone_range(zones)
        zone_str = f"Z{min(zones)}-{max(zones)}" if zones else "Z2"

        # Build workout type display name (capitalize and remove underscores)
        type_display = workout_type.replace('_', ' ').title()
        # Clean up common names
        if "Endurance" in type_display:
            type_display = "Easy"
        elif "Long Run" in type_display:
            type_display = "Long"
        elif "Rest Or Recovery" in type_display:
            type_display = "Recovery"

        # Build workout name from data
        # Format: "Week X - day - [duration] [type] [zones] [(activities)]"
        workout_name = f"Week {week_num} - {day_name} - {duration_display} {type_display} {zone_str} (Ski/SkiErg/Run)"

        return {
            "workout_name": workout_name,
            "day": day_name.capitalize(),
            "week": week_num,
            "workout_type": workout_type,
            "sport_type": "running",  # Use running for calendar display
            "duration_min": duration,
            "intensity_zones": zones,
            "hr_range": hr_range,
            "modality": "running"
        }

    def _generate_interval_workout(self, day_name: str, week_num: int, day_workout: DayWorkout) -> Dict:
        """Generate structured interval workout from program.json structure.

        Used for: vo2max_intervals, threshold_intervals

        Args:
            day_name: Name of day
            week_num: Week number
            day_workout: DayWorkout model with structure

        Returns:
            Workout dictionary
        """
        structure = day_workout.structure
        workout_type = day_workout.workout_type

        # Determine variant based on workout type
        if workout_type == "vo2max_intervals":
            variant_letter = self._get_vo2max_variant(week_num, day_name.lower())
        elif workout_type == "threshold_intervals":
            variant_letter = self._get_threshold_variant(week_num)
        else:
            variant_letter = "A"  # Default fallback

        variant = next(v for v in structure.main_set_variants if v.variant == variant_letter)

        workout_name = f"Week {week_num} - {day_name} - {workout_type.replace('_intervals', '').replace('_', ' ').title()} {variant_letter}"

        return {
            "workout_name": workout_name,
            "day": day_name.capitalize(),
            "week": week_num,
            "workout_type": workout_type,
            "sport_type": self._get_sport_type(day_workout.modality),
            "variant": variant_letter,
            "warmup": {
                "duration_min": structure.warmup_duration_min,
                "hr_range": self.hr_zones[structure.warmup_intensity_zone]
            },
            "main_set": {
                "intervals": variant.intervals,
                "work_duration_min": variant.work_duration_min,
                "rest_duration_min": variant.rest_duration_min,
                "work_hr_range": self._get_zone_range(structure.work_intensity_zones),
                "rest_hr_range": self._get_zone_range(structure.rest_intensity_zones)
            },
            "cooldown": {
                "duration_min": structure.cooldown_duration_min,
                "hr_range": self._get_zone_range(structure.cooldown_intensity_zones)
            },
            "modality": day_workout.modality
        }

    def _get_sport_type(self, modality: Optional[str]) -> str:
        """Determine Garmin sport type based on modality.

        Args:
            modality: Modality string from workout

        Returns:
            Garmin sport type: 'running', 'ski_erg', or 'other'
        """
        if not modality:
            return "other"

        modality_lower = modality.lower()

        if "running" in modality_lower or "treadmill" in modality_lower:
            return "running"

        if "ski_erg" in modality_lower:
            return "ski_erg"

        return "other"

    def _get_zone_range(self, zones: list) -> Dict[str, int]:
        """Get HR range spanning multiple zones.

        Args:
            zones: List of zone numbers

        Returns:
            Dict with min and max HR values
        """
        min_hr = min(self.hr_zones[z]["min"] for z in zones)
        max_hr = max(self.hr_zones[z]["max"] for z in zones)
        return {"min": min_hr, "max": max_hr}


def load_program(filepath: str = "program.json") -> Program:
    """Load program from JSON file.

    Args:
        filepath: Path to program JSON file

    Returns:
        Program model
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return Program(**data)
