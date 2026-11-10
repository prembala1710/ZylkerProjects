"""The order of the child elements of the MSPDI elements that the export writes.

Microsoft Project is strict about this order, so the export is checked against it. The lists below are the element names
in schema order, taken from MPXJ 16.11.0 (``org.mpxj.mspdi.schema``: the ``@XmlType(propOrder)`` of ``Project``,
``Project.Tasks.Task``, ``...Task.PredecessorLink``, ``Project.Resources.Resource``, ``Project.Assignments.Assignment``
(up to the numbered custom fields at its end), ``Project.Calendars.Calendar``, ``...WeekDay`` and ``...WorkingTime``),
which MPXJ generates from Microsoft's MSPDI schema.
"""

ORDER = {
    "Project": (
        "SaveVersion UID Name GUID Title Subject Category Company Manager Author CreationDate Revision LastSaved "
        "ScheduleFromStart StartDate FinishDate FYStartDate CriticalSlackLimit CurrencyDigits CurrencySymbol CurrencyCode "
        "CurrencySymbolPosition CalendarUID DefaultStartTime DefaultFinishTime MinutesPerDay MinutesPerWeek DaysPerMonth "
        "DefaultTaskType DefaultFixedCostAccrual DefaultStandardRate DefaultOvertimeRate DurationFormat WorkFormat "
        "EditableActualCosts HonorConstraints EarnedValueMethod InsertedProjectsLikeSummary MultipleCriticalPaths "
        "NewTasksEffortDriven NewTasksEstimated SplitsInProgressTasks SpreadActualCost SpreadPercentComplete "
        "TaskUpdatesResource FiscalYearStart WeekStartDay MoveCompletedEndsBack MoveRemainingStartsBack "
        "MoveRemainingStartsForward MoveCompletedEndsForward BaselineForEarnedValue AutoAddNewResourcesAndTasks StatusDate "
        "CurrentDate MicrosoftProjectServerURL Autolink NewTaskStartDate NewTasksAreManual DefaultTaskEVMethod "
        "ProjectExternallyEdited ExtendedCreationDate ActualsInSync RemoveFileProperties AdminProject BaselineCalendar "
        "UpdateManuallyScheduledTasksWhenEditingLinks KeepTaskOnNearestWorkingTimeWhenMadeAutoScheduled OutlineCodes WBSMasks "
        "ExtendedAttributes Calendars Tasks Resources Assignments"
    ),
    "Task": (
        "UID GUID ID Name Active Manual Type IsNull CreateDate Contact WBS WBSLevel OutlineNumber OutlineLevel Priority Start "
        "Finish Duration DurationFormat Work Stop Resume ResumeValid EffortDriven Recurring OverAllocated Estimated Milestone "
        "Summary DisplayAsSummary Critical IsSubproject IsSubprojectReadOnly SubprojectName ExternalTask ExternalTaskProject "
        "EarlyStart EarlyFinish LateStart LateFinish StartVariance FinishVariance WorkVariance FreeSlack TotalSlack StartSlack "
        "FinishSlack FixedCost FixedCostAccrual PercentComplete PercentWorkComplete Cost OvertimeCost OvertimeWork ActualStart "
        "ActualFinish ActualDuration ActualCost ActualOvertimeCost ActualWork ActualOvertimeWork RegularWork RemainingDuration "
        "RemainingCost RemainingWork RemainingOvertimeCost RemainingOvertimeWork ACWP CV ConstraintType CalendarUID "
        "ConstraintDate Deadline LevelAssignments LevelingCanSplit LevelingDelay LevelingDelayFormat PreLeveledStart "
        "PreLeveledFinish Hyperlink HyperlinkAddress HyperlinkSubAddress IgnoreResourceCalendar Notes HideBar Rollup BCWS BCWP "
        "PhysicalPercentComplete EarnedValueMethod PredecessorLink ActualWorkProtected ActualOvertimeWorkProtected "
        "ExtendedAttribute Baseline OutlineCode IsPublished StatusManager CommitmentStart CommitmentFinish CommitmentType "
        "StartText FinishText DurationText ManualStart ManualFinish ManualDuration TimephasedData Project"
    ),
    "PredecessorLink": (
        "PredecessorUID Type CrossProject CrossProjectName LinkLag LagFormat"
    ),
    "Resource": (
        "UID GUID ID Name Type IsNull Initials Phonetics NTAccount MaterialLabel Code Group WorkGroup EmailAddress Hyperlink "
        "HyperlinkAddress HyperlinkSubAddress MaxUnits PeakUnits OverAllocated AvailableFrom AvailableTo Start Finish CanLevel "
        "AccrueAt Work RegularWork OvertimeWork ActualWork RemainingWork ActualOvertimeWork RemainingOvertimeWork "
        "PercentWorkComplete StandardRate StandardRateFormat Cost OvertimeRate OvertimeRateFormat OvertimeCost CostPerUse "
        "ActualCost ActualOvertimeCost RemainingCost RemainingOvertimeCost WorkVariance CostVariance SV CV ACWP CalendarUID "
        "Notes BCWS BCWP IsGeneric IsInactive IsEnterprise BookingType ActualWorkProtected ActualOvertimeWorkProtected "
        "ActiveDirectoryGUID CreationDate ExtendedAttribute Baseline OutlineCode CostCenter IsCostResource AssnOwner "
        "AssnOwnerGuid IsBudget AvailabilityPeriods Rates TimephasedData"
    ),
    "Assignment": (
        "UID GUID TaskUID ResourceUID PercentWorkComplete ActualCost ActualFinish ActualOvertimeCost ActualOvertimeWork "
        "ActualStart ActualWork ACWP Confirmed Cost CostRateTable CostVariance CV Delay Finish FinishVariance Hyperlink "
        "HyperlinkAddress HyperlinkSubAddress WorkVariance HasFixedRateUnits FixedMaterial LevelingDelay LevelingDelayFormat "
        "LinkedFields Milestone Notes Overallocated OvertimeCost OvertimeWork PeakUnits RateScale RegularWork RemainingCost "
        "RemainingOvertimeCost RemainingOvertimeWork RemainingWork ResponsePending Start Stop Resume StartVariance Summary SV "
        "Units UpdateNeeded VAC Work WorkContour BCWS BCWP BookingType"
    ),
    "Calendar": (
        "UID GUID Name IsBaseCalendar IsBaselineCalendar BaseCalendarUID WeekDays Exceptions WorkWeeks"
    ),
    "WeekDay": (
        "DayType DayWorking TimePeriod WorkingTimes"
    ),
    "WorkingTime": (
        "FromTime ToTime"
    ),
}  # fmt: skip
ORDER = {name: tuple(names.split()) for name, names in ORDER.items()}

CONTAINERS = {"Project": "Project", "Task": "Task", "PredecessorLink": "PredecessorLink", "Resource": "Resource",
              "Assignment": "Assignment", "Calendar": "Calendar", "WeekDay": "WeekDay", "WorkingTime": "WorkingTime"}  # fmt: skip


def local(tag):
    return tag.rpartition("}")[2]


def order_problems(root):
    """Everything in ``root`` (an ElementTree element) that is out of schema order, or not in the schema at all."""
    problems = []
    for element in root.iter():
        name = local(element.tag)
        if name not in ORDER:
            continue
        expected = ORDER[name]
        seen = -1
        for child in element:
            child_name = local(child.tag)
            if child_name not in expected:
                problems.append("<%s> is not a child of <%s> in the schema" % (child_name, name))
                continue
            position = expected.index(child_name)
            if position < seen:
                problems.append("<%s> comes after a later element inside <%s>" % (child_name, name))
            seen = max(seen, position)
    return problems
