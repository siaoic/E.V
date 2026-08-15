extends NeuroAction

const TicTacToe := preload("res://examples/tic_tac_toe.gd")

var _ticTacToe: TicTacToe

func _init(window: ActionWindow, ticTacToe: TicTacToe):
	super(window)
	_ticTacToe = ticTacToe

func _get_name() -> String:
	return "play"

func _get_description() -> String:
	return "Place an O in the specified cell."

func _get_schema() -> Dictionary:
	return JsonUtils.wrap_schema({
		"cell": {
			"enum": _get_available_cells()
		}
	})

func _validate_action(data: IncomingData, state: Dictionary) -> ExecutionResult:
	var cell := data.get_string("cell")
	if not cell:
		return ExecutionResult.failure(Strings.action_failed_missing_required_parameter.format(["cell"]))

	var cells := _get_available_cells()
	if not cells.has(cell):
		return ExecutionResult.failure(Strings.action_failed_invalid_parameter.format(["cell"]))

	state["cell"] = _ticTacToe.container.find_child(cell, false)
	return ExecutionResult.success()

func _execute_action(state: Dictionary) -> void:
	_ticTacToe.bot_play_in_cell(state["cell"])

func _get_available_cells() -> Array[String]:
	var result: Array[String] = []
	for child in _ticTacToe.container.get_children():
		if child is BaseButton:
			if ((child.get_child(0) as Control).visible == false &&
				(child.get_child(1) as Control).visible == false):
				result.append(child.name)
	return result
