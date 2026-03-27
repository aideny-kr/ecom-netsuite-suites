"""Tests for follow-up intent classification."""

from app.services.chat.follow_up_classifier import FollowUpIntent, classify_follow_up


class TestFollowUpClassifier:
    def test_chart_that_is_transform(self):
        assert classify_follow_up("chart that", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_make_a_bar_chart_is_transform(self):
        assert classify_follow_up("make a bar chart", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_pivot_this_by_month_is_transform(self):
        assert classify_follow_up("pivot this by month", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_export_this_to_csv_is_transform(self):
        assert classify_follow_up("export this to csv", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_show_me_a_different_chart_is_transform(self):
        assert classify_follow_up("show me a different chart", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_visualize_that_data_is_transform(self):
        assert classify_follow_up("visualize that data", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_sort_that_by_amount_is_transform(self):
        assert classify_follow_up("sort that by amount", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_top_10_from_that_is_transform(self):
        assert classify_follow_up("top 10 from that", has_previous_result=True) == FollowUpIntent.TRANSFORM

    def test_new_question_is_new_data(self):
        assert classify_follow_up("show me the balance sheet", has_previous_result=True) == FollowUpIntent.NEW_DATA

    def test_different_period_overrides_transform(self):
        assert classify_follow_up("chart that but for Q3", has_previous_result=True) == FollowUpIntent.NEW_DATA

    def test_different_time_range_is_new_data(self):
        assert classify_follow_up("chart this for last month", has_previous_result=True) == FollowUpIntent.NEW_DATA

    def test_no_previous_result_always_new_data(self):
        assert classify_follow_up("chart that", has_previous_result=False) == FollowUpIntent.NEW_DATA

    def test_simple_greeting_is_new_data(self):
        assert classify_follow_up("hello", has_previous_result=True) == FollowUpIntent.NEW_DATA

    def test_how_many_orders_is_new_data(self):
        assert classify_follow_up("how many orders this month?", has_previous_result=True) == FollowUpIntent.NEW_DATA

    def test_line_chart_instead_is_transform(self):
        assert (
            classify_follow_up("make it a line chart instead of bar", has_previous_result=True)
            == FollowUpIntent.TRANSFORM
        )
