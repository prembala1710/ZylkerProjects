import java.time.LocalDateTime;
import org.mpxj.*;
import org.mpxj.writer.FileFormat;
import org.mpxj.writer.UniversalProjectWriter;

/** Writes tests/fixtures/mpxj_sample.xml: a small plan saved as MSPDI by MPXJ (not by this application). */
public class MakeMpxjSample {
    public static void main(String[] args) throws Exception {
        ProjectFile file = new ProjectFile();
        ProjectProperties props = file.getProjectProperties();
        props.setName("Office move");
        props.setProjectTitle("Office move");
        props.setStartDate(LocalDateTime.of(2026, 11, 2, 8, 0));
        props.setMinutesPerDay(480);
        ProjectCalendar calendar = file.addDefaultBaseCalendar();
        file.setDefaultCalendar(calendar);

        Resource anna = file.addResource();
        anna.setName("Anna Keller");
        anna.setEmailAddress("anna@example.com");
        Resource ben = file.addResource();
        ben.setName("Ben Ortiz");
        Resource crane = file.addResource();
        crane.setName("Crane");
        crane.setType(ResourceType.MATERIAL);

        Task plan = file.addTask();
        plan.setName("Planning");
        plan.setNotes("Everything before the boxes come out.");
        Task survey = plan.addTask();
        survey.setName("Survey the new floor");
        survey.setDuration(Duration.getInstance(2, TimeUnit.DAYS));
        survey.setStart(LocalDateTime.of(2026, 11, 2, 8, 0));
        survey.setFinish(LocalDateTime.of(2026, 11, 3, 17, 0));
        survey.setPercentageComplete(100.0);
        survey.setPriority(Priority.getInstance(Priority.HIGH));
        ResourceAssignment a1 = survey.addResourceAssignment(anna);
        a1.setWork(Duration.getInstance(16, TimeUnit.HOURS));
        Task budget = plan.addTask();
        budget.setName("Approve the budget");
        budget.setDuration(Duration.getInstance(3, TimeUnit.DAYS));
        budget.setStart(LocalDateTime.of(2026, 11, 4, 8, 0));
        budget.setFinish(LocalDateTime.of(2026, 11, 6, 17, 0));
        budget.setPercentageComplete(40.0);
        budget.setPriority(Priority.getInstance(Priority.LOWEST));
        ResourceAssignment a2 = budget.addResourceAssignment(ben);
        a2.setWork(Duration.getInstance(12, TimeUnit.HOURS));
        ResourceAssignment a3 = budget.addResourceAssignment(anna);
        a3.setWork(Duration.getInstance(4, TimeUnit.HOURS));
        budget.addPredecessor(new Relation.Builder().predecessorTask(survey).type(RelationType.FINISH_START).lag(Duration.getInstance(1, TimeUnit.DAYS)));

        Task move = file.addTask();
        move.setName("Move day");
        Task pack = move.addTask();
        pack.setName("Pack the offices");
        pack.setDuration(Duration.getInstance(4, TimeUnit.DAYS));
        pack.setStart(LocalDateTime.of(2026, 11, 9, 8, 0));
        pack.setFinish(LocalDateTime.of(2026, 11, 12, 17, 0));
        pack.setDeadline(LocalDateTime.of(2026, 11, 13, 17, 0));
        pack.addPredecessor(new Relation.Builder().predecessorTask(budget).type(RelationType.FINISH_START));
        Task haul = move.addTask();
        haul.setName("Haul the furniture");
        haul.setDuration(Duration.getInstance(1, TimeUnit.DAYS));
        haul.setStart(LocalDateTime.of(2026, 11, 13, 8, 0));
        haul.setFinish(LocalDateTime.of(2026, 11, 13, 17, 0));
        haul.setConstraintType(ConstraintType.START_NO_EARLIER_THAN);
        haul.setConstraintDate(LocalDateTime.of(2026, 11, 13, 8, 0));
        haul.setPercentageComplete(0.0);
        haul.addResourceAssignment(crane);
        haul.addPredecessor(new Relation.Builder().predecessorTask(pack).type(RelationType.START_START).lag(Duration.getInstance(2, TimeUnit.DAYS)));
        Task unpack = move.addTask();
        unpack.setName("Unpack & settle in");
        unpack.setDuration(Duration.getInstance(2, TimeUnit.DAYS));
        unpack.setStart(LocalDateTime.of(2026, 11, 16, 8, 0));
        unpack.setFinish(LocalDateTime.of(2026, 11, 17, 17, 0));
        unpack.addPredecessor(new Relation.Builder().predecessorTask(haul).type(RelationType.FINISH_FINISH));

        Task done = file.addTask();
        done.setName("Moved in");
        done.setMilestone(true);
        done.setDuration(Duration.getInstance(0, TimeUnit.DAYS));
        done.setStart(LocalDateTime.of(2026, 11, 17, 17, 0));
        done.setFinish(LocalDateTime.of(2026, 11, 17, 17, 0));
        done.addPredecessor(new Relation.Builder().predecessorTask(unpack).type(RelationType.FINISH_START));
        Task party = file.addTask();
        party.setName("Housewarming");
        party.setDuration(Duration.getInstance(1, TimeUnit.DAYS));
        party.setStart(LocalDateTime.of(2026, 11, 20, 8, 0));
        party.setFinish(LocalDateTime.of(2026, 11, 20, 17, 0));
        party.addPredecessor(new Relation.Builder().predecessorTask(done).type(RelationType.FINISH_START).lag(Duration.getInstance(2, TimeUnit.DAYS)));

        file.updateStructure();
        new UniversalProjectWriter(FileFormat.MSPDI).write(file, args[0]);
    }
}
